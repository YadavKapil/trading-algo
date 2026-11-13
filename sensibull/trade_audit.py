"""Read-only reconstruction of net position changes, not broker executions."""

from collections import defaultdict, deque
from datetime import datetime
from statistics import median
from zoneinfo import ZoneInfo


IST = ZoneInfo("Asia/Kolkata")


def local_time(value):
    if not value:
        return None
    result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    return (
        result.replace(tzinfo=IST)
        if result.tzinfo is None
        else result.astimezone(IST)
    )


def iso_time(value):
    parsed = local_time(value)
    return parsed.isoformat() if parsed else None


def match_quantities(events):
    """FIFO is an explicit attribution convention; baseline entries are unknown."""
    inventory = defaultdict(deque)
    matches = []
    for event in events:
        key = (event["trading_symbol"], event["product"])
        lots = inventory[key]
        previous = event["previous_quantity"]
        signed_inventory = sum(lot["signed_quantity"] for lot in lots)
        if signed_inventory != previous:
            lots.clear()
            if previous:
                lots.append({"signed_quantity": previous, "entry": None})

        remaining = event["quantity_delta"]
        while lots and remaining and lots[0]["signed_quantity"] * remaining < 0:
            lot = lots[0]
            matched = min(abs(remaining), abs(lot["signed_quantity"]))
            sign = 1 if lot["signed_quantity"] > 0 else -1
            entry = lot["entry"]
            matches.append({
                "underlying": event["underlying"],
                "trading_symbol": event["trading_symbol"],
                "product": event["product"],
                "direction": "LONG" if sign > 0 else "SHORT",
                "quantity": matched,
                "entry": entry,
                "exit": event,
                "state": "MATCHED" if entry else "BASELINE EXIT",
            })
            lot["signed_quantity"] -= sign * matched
            remaining += sign * matched
            if not lot["signed_quantity"]:
                lots.popleft()
        if remaining:
            lots.append({"signed_quantity": remaining, "entry": event})

    for key, lots in inventory.items():
        for lot in lots:
            entry = lot["entry"]
            matches.append({
                "underlying": entry["underlying"] if entry else None,
                "trading_symbol": key[0],
                "product": key[1],
                "direction": "LONG" if lot["signed_quantity"] > 0 else "SHORT",
                "quantity": abs(lot["signed_quantity"]),
                "entry": entry,
                "exit": None,
                "state": "OPEN" if entry else "BASELINE OPEN",
            })
    return matches


def build_audit(event_rows, snapshot_maps, positions, day):
    events = []
    first_events = {}
    last_events = {}
    discontinuities = []
    for row in event_rows:
        source = local_time(row["source_updated_at"] or row["observed_at"])
        if source.date().isoformat() > day:
            continue
        quality = row["timing_quality"] or "source_time_only"
        detected = local_time(row["detected_at"]) if quality == "recorded" else None
        previous = local_time(row["previous_updated_at"])
        snapshot_positions = snapshot_maps.get(row["snapshot_id"], {})
        key = (row["trading_symbol"], row["product"])
        last = last_events.get(key)
        if last and last["new_quantity"] != row["previous_quantity"]:
            discontinuities.append({
                "trading_symbol": row["trading_symbol"],
                "source_updated_at": iso_time(source),
                "previous_recorded_quantity": last["new_quantity"],
                "next_observed_quantity": row["previous_quantity"],
            })
        position = snapshot_positions.get(key, {})
        info = position.get("instrument_info", {})
        delta = row["new_quantity"] - row["previous_quantity"]
        end = detected or source
        valid_window = previous is not None and previous < source <= end
        event = {
            "id": row["id"],
            "snapshot_id": row["snapshot_id"],
            "underlying": row["underlying"],
            "trading_symbol": row["trading_symbol"],
            "product": row["product"],
            "expiry": info.get("expiry"),
            "strike": info.get("strike"),
            "option_type": info.get("instrument_type"),
            "action": "BUY" if delta > 0 else "SELL",
            "quantity": abs(delta),
            "previous_quantity": row["previous_quantity"],
            "new_quantity": row["new_quantity"],
            "quantity_delta": delta,
            "event_type": row["event_type"],
            "previous_updated_at": iso_time(previous),
            "source_updated_at": iso_time(source),
            "detected_at": iso_time(detected),
            "timing_quality": quality,
            "time": int(end.timestamp()),
            "previous_time": int(previous.timestamp()) if valid_window else None,
            "window_seconds": (end - previous).total_seconds() if valid_window else None,
            "source_delay_seconds": (detected - source).total_seconds() if detected else None,
            "previous_underlying_price": row["previous_underlying_price"],
            "current_underlying_price": row["current_underlying_price"],
            "option_quote": position.get("last_price"),
            "gap": valid_window and (end - previous).total_seconds() > 180,
            "after_hours": not ("09:15" <= source.strftime("%H:%M") < "15:30"),
        }
        events.append(event)
        # A reversal or a new opening starts a new observed position lifecycle.
        if (
            key not in first_events
            or row["previous_quantity"] == 0
            or row["previous_quantity"] * row["new_quantity"] < 0
        ):
            first_events[key] = event
        last_events[key] = event

    for position in positions:
        key = (position["trading_symbol"], position["product"])
        first = first_events.get(key)
        last = last_events.get(key)
        position["first_detected_at"] = first["detected_at"] if first else None
        position["previous_updated_at"] = first["previous_updated_at"] if first else None
        position["last_changed_at"] = last["detected_at"] if last else None
        position["first_source_at"] = first["source_updated_at"] if first else None
        position["timing_quality"] = first["timing_quality"] if first else "baseline"

    selected = [
        event for event in events
        if local_time(event["source_updated_at"]).date().isoformat() == day
    ]
    matches = match_quantities(events)
    position_map = {
        (position["trading_symbol"], position["product"]): position
        for position in positions
    }
    open_keys = {
        (match["trading_symbol"], match["product"])
        for match in matches if match["exit"] is None
    }
    for key, position in position_map.items():
        if position["quantity"] and key not in open_keys:
            matches.append({
                "underlying": position["underlying"],
                "trading_symbol": position["trading_symbol"],
                "product": position["product"],
                "direction": "LONG" if position["quantity"] > 0 else "SHORT",
                "quantity": abs(position["quantity"]),
                "entry": None,
                "exit": None,
                "state": "BASELINE OPEN",
            })
    selected_matches = []
    for match in matches:
        if match["underlying"] is None:
            position = position_map.get((match["trading_symbol"], match["product"]))
            match["underlying"] = position["underlying"] if position else "UNKNOWN"
        entry = match["entry"]
        exit_event = match["exit"]
        if any(
            item and local_time(item["source_updated_at"]).date().isoformat() == day
            for item in (entry, exit_event)
        ) or exit_event is None:
            selected_matches.append(match)

    windows = [event["window_seconds"] for event in selected if event["window_seconds"] is not None]
    delays = [event["source_delay_seconds"] for event in selected if event["source_delay_seconds"] is not None]
    return {
        "events": selected,
        "matches": selected_matches,
        "positions": positions,
        "discontinuities": discontinuities,
        "summary": {
            "changes": len(selected),
            "source_updates": len({e["source_updated_at"] for e in selected}),
            "buy_quantity": sum(e["quantity"] for e in selected if e["action"] == "BUY"),
            "sell_quantity": sum(e["quantity"] for e in selected if e["action"] == "SELL"),
            "median_window_seconds": median(windows) if windows else None,
            "max_window_seconds": max(windows) if windows else None,
            "median_source_delay_seconds": median(delays) if delays else None,
            "gap_events": sum(bool(e["gap"]) for e in selected),
            "unknown_detection_events": sum(e["detected_at"] is None for e in selected),
            "matched_quantity": sum(m["quantity"] for m in selected_matches if m["state"] == "MATCHED"),
            "baseline_exit_quantity": sum(m["quantity"] for m in selected_matches if m["state"] == "BASELINE EXIT"),
        },
    }
