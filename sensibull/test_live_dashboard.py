import unittest
import json
import tempfile
from pathlib import Path
from datetime import datetime
from unittest.mock import patch

from live_dashboard import (
    calculate_position_events, parse_yahoo_candles, init_live_tables,
    store_position_snapshot, get_live_dashboard_data, store_candles,
    get_event_evidence,
)
from database import get_db
from trade_audit import match_quantities


def snapshot(quantity):
    return {
        "data": [
            {
                "trading_symbol": "NIFTY",
                "underlying_price": 23000,
                "trades": [
                    {
                        "trading_symbol": "NIFTY03NOV22800PE",
                        "product": "NRML",
                        "quantity": quantity,
                        "instrument_info": {
                            "expiry": "2026-11-03",
                            "strike": 22800,
                            "instrument_type": "Put",
                        },
                    }
                ],
            }
        ]
    }


class PositionEventTests(unittest.TestCase):
    def test_detects_new_short_position(self):
        events = calculate_position_events({"data": []}, snapshot(-325))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "SHORT_ADDED")
        self.assertEqual(events[0]["quantity_delta"], -325)
        self.assertEqual(events[0]["previous_underlying_price"], None)
        self.assertEqual(events[0]["current_underlying_price"], 23000)

    def test_detects_short_position_reduction(self):
        events = calculate_position_events(snapshot(-650), snapshot(-325))

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "SHORT_REDUCED")
        self.assertEqual(events[0]["quantity_delta"], 325)

    def test_ignores_pnl_only_changes(self):
        previous = snapshot(-325)
        current = snapshot(-325)
        previous["data"][0]["trades"][0]["unbooked_pnl"] = 100
        current["data"][0]["trades"][0]["unbooked_pnl"] = 500

        self.assertEqual(calculate_position_events(previous, current), [])


class YahooCandleTests(unittest.TestCase):
    def test_parses_complete_candles_and_skips_missing_rows(self):
        payload = {
            "chart": {
                "error": None,
                "result": [
                    {
                        "timestamp": [100, 160],
                        "indicators": {
                            "quote": [
                                {
                                    "open": [10, None],
                                    "high": [12, None],
                                    "low": [9, None],
                                    "close": [11, None],
                                    "volume": [20, None],
                                }
                            ]
                        },
                    }
                ],
            }
        }

        self.assertEqual(
            parse_yahoo_candles(payload),
            [
                {
                    "timestamp": 60,
                    "open": 10.0,
                    "high": 12.0,
                    "low": 9.0,
                    "close": 11.0,
                    "volume": 20,
                }
            ],
        )


def event(previous, current, identifier):
    return {
        "id": identifier,
        "underlying": "NIFTY",
        "trading_symbol": "NIFTY03NOV22800PE",
        "product": "NRML",
        "previous_quantity": previous,
        "new_quantity": current,
        "quantity_delta": current - previous,
    }


class QuantityTraceTests(unittest.TestCase):
    def test_partial_short_buybacks_use_fifo_and_leave_open_inventory(self):
        rows = match_quantities([event(0, -100, 1), event(-100, -150, 2), event(-150, -30, 3)])
        self.assertEqual([(r["quantity"], r["state"]) for r in rows], [
            (100, "MATCHED"), (20, "MATCHED"), (30, "OPEN"),
        ])
        self.assertEqual([r["entry"]["id"] for r in rows], [1, 2, 2])
        self.assertEqual(rows[0]["direction"], "SHORT")

    def test_long_sale_matches_purchase(self):
        rows = match_quantities([event(0, 100, 1), event(100, 0, 2)])
        self.assertEqual(rows[0]["quantity"], 100)
        self.assertEqual(rows[0]["direction"], "LONG")
        self.assertEqual(rows[0]["state"], "MATCHED")

    def test_existing_short_has_unknown_entry(self):
        rows = match_quantities([event(-100, -40, 1)])
        self.assertEqual([(r["quantity"], r["state"]) for r in rows], [
            (60, "BASELINE EXIT"), (40, "BASELINE OPEN"),
        ])
        self.assertIsNone(rows[0]["entry"])

    def test_reversal_closes_old_side_and_opens_remainder(self):
        rows = match_quantities([event(0, -100, 1), event(-100, 50, 2)])
        self.assertEqual([(r["quantity"], r["direction"], r["state"]) for r in rows], [
            (100, "SHORT", "MATCHED"), (50, "LONG", "OPEN"),
        ])

    def test_gap_resets_unobserved_inventory_to_unknown(self):
        rows = match_quantities([event(0, -100, 1), event(-50, 0, 2)])
        self.assertEqual(rows[0]["state"], "BASELINE EXIT")
        self.assertIsNone(rows[0]["entry"])


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_patch = patch("database.DB_PATH", str(Path(self.directory.name) / "audit.db"))
        self.db_patch.start()
        init_live_tables()

    def tearDown(self):
        self.db_patch.stop()
        self.directory.cleanup()

    def store(self, quantity, source="2026-11-02T04:30:00Z", detected="2026-11-02T10:00:10"):
        data = snapshot(quantity)
        data["created_at"] = source
        store_position_snapshot("test", data, datetime.fromisoformat(detected))
        return data

    def test_first_snapshot_is_baseline_not_a_fake_trade(self):
        self.store(-100)
        result = get_live_dashboard_data("test")
        self.assertEqual(result["events"], [])
        self.assertEqual(result["matches"][0]["state"], "BASELINE OPEN")
        self.assertIsNone(result["positions"][0]["first_detected_at"])

    def test_unchanged_polls_persist_and_first_detection_does_not_move(self):
        self.store(0)
        self.store(-100, "2026-11-02T04:31:00Z", "2026-11-02T10:01:10")
        self.store(-100, "2026-11-02T04:32:00Z", "2026-11-02T10:02:10")
        result = get_live_dashboard_data("test")
        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["polls_recorded"], 3)
        position = result["positions"][0]
        self.assertEqual(position["first_detected_at"], "2026-11-02T10:01:10+05:30")
        self.assertEqual(position["previous_updated_at"], "2026-11-02T10:00:00+05:30")

    def test_source_time_updates_alone_do_not_create_trades(self):
        self.store(-100)
        self.store(-100, "2026-11-02T04:31:00Z", "2026-11-02T10:01:10")
        self.store(-100, "2026-11-02T04:32:00Z", "2026-11-02T10:02:10")
        result = get_live_dashboard_data("test")
        self.assertEqual(result["events"], [])
        self.assertIsNone(result["positions"][0]["first_detected_at"])

    def test_evidence_keeps_source_time_and_detection_distinct(self):
        self.store(0)
        self.store(-100, "2026-11-02T04:31:00Z", "2026-11-02T10:01:10")
        event = get_live_dashboard_data("test")["events"][0]
        evidence = get_event_evidence(event["id"])
        self.assertTrue(evidence["quantity_verified"])
        self.assertEqual(evidence["before"]["quantity"], 0)
        self.assertEqual(evidence["after"]["quantity"], -100)
        self.assertEqual(evidence["after"]["source_updated_at"], "2026-11-02T10:01:00+05:30")
        self.assertEqual(evidence["first_detected_at"], "2026-11-02T10:01:10+05:30")

    def test_after_midnight_still_defaults_to_source_session(self):
        self.store(0)
        self.store(-100, "2026-11-02T04:31:00Z", "2026-11-02T10:01:10")
        result = get_live_dashboard_data("test")
        self.assertEqual(result["date"], "2026-11-02")
        self.assertEqual(len(result["events"]), 1)

    def test_selected_date_does_not_show_latest_future_positions(self):
        self.store(-100)
        self.store(-200, "2026-11-03T04:31:00Z", "2026-11-03T10:01:10")
        result = get_live_dashboard_data("test", "2026-11-02")
        self.assertEqual(result["positions"][0]["quantity"], -100)
        self.assertEqual(result["events"], [])

    def test_cross_session_change_is_recorded_with_wide_gap_not_discarded(self):
        self.store(-100)
        self.store(-200, "2026-11-03T04:31:00Z", "2026-11-03T10:01:10")
        result = get_live_dashboard_data("test", "2026-11-03")
        self.assertEqual(result["events"][0]["quantity"], 100)
        self.assertTrue(result["events"][0]["gap"])

    def test_rejects_stale_snapshot_without_replacing_latest(self):
        self.store(-100)
        with self.assertRaisesRegex(ValueError, "older snapshot"):
            self.store(-200, "2026-11-01T04:31:00Z")
        self.assertEqual(get_live_dashboard_data("test")["positions"][0]["quantity"], -100)

    def test_missing_source_time_is_an_explicit_failure(self):
        with self.assertRaisesRegex(ValueError, "created_at"):
            store_position_snapshot("test", snapshot(-100))

    def test_quantity_reversal_has_correct_net_action(self):
        self.store(-100)
        self.store(50, "2026-11-02T04:31:00Z", "2026-11-02T10:01:10")
        result = get_live_dashboard_data("test")
        self.assertEqual(result["events"][0]["action"], "BUY")
        self.assertEqual(result["events"][0]["quantity"], 150)

    def test_close_does_not_replace_first_observed_entry_time(self):
        self.store(0)
        self.store(-100, "2026-11-02T04:31:00Z", "2026-11-02T10:01:10")
        self.store(0, "2026-11-02T04:32:00Z", "2026-11-02T10:02:10")
        result = get_live_dashboard_data("test")
        self.assertEqual(result["positions"][0]["first_detected_at"], "2026-11-02T10:01:10+05:30")
        self.assertEqual(result["positions"][0]["last_changed_at"], "2026-11-02T10:02:10+05:30")
        self.assertEqual(result["matches"][0]["quantity"], 100)

    def test_legacy_time_is_not_claimed_as_local_detection(self):
        self.store(0)
        self.store(-100, "2026-11-02T04:31:00Z", "2026-11-02T10:01:10")
        conn = get_db()
        conn.execute("UPDATE live_position_events SET timing_quality = 'source_time_only'")
        conn.commit()
        conn.close()
        result = get_live_dashboard_data("test")
        self.assertIsNone(result["events"][0]["detected_at"])
        self.assertIsNone(result["positions"][0]["first_detected_at"])

    def test_candles_are_minute_aligned_and_exclude_after_hours(self):
        self.store(-100)
        stamp = int(datetime.fromisoformat("2026-11-02T10:00:00+05:30").timestamp())
        after = int(datetime.fromisoformat("2026-11-02T22:00:00+05:30").timestamp())
        store_candles([
            dict(timestamp=t, open=10, high=12, low=9, close=11, volume=0)
            for t in (stamp, stamp + 5, after)
        ])
        result = get_live_dashboard_data("test")
        self.assertEqual([c["time"] for c in result["candles"]], [stamp])


if __name__ == "__main__":
    unittest.main()
