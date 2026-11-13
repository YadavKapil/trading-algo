import json
import logging
import os
import threading
import time
from datetime import date, datetime, time as datetime_time
from zoneinfo import ZoneInfo

import requests

from database import get_db, init_db
from trade_audit import build_audit


logger = logging.getLogger(__name__)

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_PROFILE_SLUG = os.getenv("SENSIBULL_LIVE_SLUG", "latered-garage")
SENSIBULL_URL = (
    "https://web.sensibull.com/verified-pnl/"
    f"{DEFAULT_PROFILE_SLUG}/live-positions?vbs_src=vbs-long-term-pnl"
)
SENSIBULL_RESPONSE_PATH = "/verified_by_sensibull/live_positions/snapshot/"
YAHOO_NIFTY_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI"
    "?interval=1m&range=1d"
)

_collector_thread = None
_collector_lock = threading.Lock()


def _configure_connection(conn):
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")


def init_live_tables():
    init_db()
    conn = get_db()
    _configure_connection(conn)
    cursor = conn.cursor()
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS market_candles (
            symbol TEXT NOT NULL,
            timestamp INTEGER NOT NULL,
            open REAL NOT NULL,
            high REAL NOT NULL,
            low REAL NOT NULL,
            close REAL NOT NULL,
            volume REAL,
            PRIMARY KEY (symbol, timestamp)
        )
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS live_position_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            snapshot_id INTEGER NOT NULL,
            observed_at DATETIME NOT NULL,
            previous_updated_at DATETIME,
            source_updated_at DATETIME,
            detected_at DATETIME,
            underlying TEXT NOT NULL,
            trading_symbol TEXT NOT NULL,
            product TEXT NOT NULL,
            previous_quantity INTEGER NOT NULL,
            new_quantity INTEGER NOT NULL,
            quantity_delta INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            previous_underlying_price REAL,
            current_underlying_price REAL,
            UNIQUE(profile_id, observed_at, trading_symbol, product),
            FOREIGN KEY (profile_id) REFERENCES profiles (id),
            FOREIGN KEY (snapshot_id) REFERENCES snapshots (id)
        )
        """
    )
    event_columns = {
        row["name"]
        for row in cursor.execute(
            "PRAGMA table_info(live_position_events)"
        ).fetchall()
    }
    event_migrations = {
        "previous_updated_at": "DATETIME",
        "source_updated_at": "DATETIME",
        "detected_at": "DATETIME",
        "previous_underlying_price": "REAL",
        "current_underlying_price": "REAL",
        "timing_quality": "TEXT",
    }
    for column, column_type in event_migrations.items():
        if column not in event_columns:
            cursor.execute(
                f"ALTER TABLE live_position_events "
                f"ADD COLUMN {column} {column_type}"
            )
    cursor.execute(
        """
        UPDATE live_position_events
        SET timing_quality = CASE
            WHEN detected_at IS NULL OR detected_at = observed_at
                THEN 'source_time_only'
            ELSE 'recorded'
        END
        WHERE timing_quality IS NULL
        """
    )
    cursor.execute(
        """
        UPDATE live_position_events
        SET previous_updated_at = (
            SELECT MAX(previous_snapshot.timestamp)
            FROM snapshots AS previous_snapshot
            WHERE previous_snapshot.profile_id =
                    live_position_events.profile_id
              AND previous_snapshot.id < live_position_events.snapshot_id
        )
        WHERE previous_updated_at IS NULL
        """
    )
    cursor.execute(
        """
        UPDATE live_position_events
        SET source_updated_at = observed_at
        WHERE source_updated_at IS NULL
        """
    )
    cursor.execute(
        """
        UPDATE live_position_events
        SET detected_at = observed_at
        WHERE detected_at IS NULL
        """
    )
    legacy_events = cursor.execute(
        """
        SELECT id, profile_id, snapshot_id, underlying
        FROM live_position_events
        WHERE previous_underlying_price IS NULL
           OR current_underlying_price IS NULL
        """
    ).fetchall()
    for event in legacy_events:
        current_snapshot = cursor.execute(
            "SELECT raw_data FROM snapshots WHERE id = ?",
            (event["snapshot_id"],),
        ).fetchone()
        previous_snapshot = cursor.execute(
            """
            SELECT raw_data
            FROM snapshots
            WHERE profile_id = ? AND id < ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (event["profile_id"], event["snapshot_id"]),
        ).fetchone()
        if not current_snapshot or not previous_snapshot:
            continue

        current_price = _underlying_price(
            json.loads(current_snapshot["raw_data"]),
            event["underlying"],
        )
        previous_price = _underlying_price(
            json.loads(previous_snapshot["raw_data"]),
            event["underlying"],
        )
        cursor.execute(
            """
            UPDATE live_position_events
            SET previous_underlying_price = COALESCE(
                    previous_underlying_price, ?
                ),
                current_underlying_price = COALESCE(
                    current_underlying_price, ?
                )
            WHERE id = ?
            """,
            (previous_price, current_price, event["id"]),
        )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS live_polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            profile_id INTEGER NOT NULL,
            source_updated_at DATETIME NOT NULL,
            detected_at DATETIME NOT NULL,
            raw_data TEXT NOT NULL
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS live_polls_profile_time
        ON live_polls(profile_id, detected_at)
        """
    )
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS collector_status (
            source TEXT PRIMARY KEY,
            last_attempt DATETIME,
            last_success DATETIME,
            status TEXT NOT NULL,
            message TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def _position_key(trade):
    return trade.get("trading_symbol"), trade.get("product", "")


def _underlying_price(snapshot, underlying):
    for item in snapshot.get("data", []):
        if item.get("trading_symbol") == underlying:
            return item.get("underlying_price")
    return None


def normalize_positions(snapshot):
    positions = {}
    for underlying in snapshot.get("data", []):
        underlying_name = underlying.get("trading_symbol", "UNKNOWN")
        underlying_price = underlying.get("underlying_price")
        for trade in underlying.get("trades", []):
            key = _position_key(trade)
            if not key[0] or trade.get("quantity") is None:
                raise ValueError("Sensibull position is missing its symbol or quantity")
            quantity = int(trade["quantity"])
            if key not in positions:
                positions[key] = {
                    "underlying": underlying_name,
                    "underlying_price": underlying_price,
                    "trading_symbol": trade.get("trading_symbol"),
                    "product": trade.get("product", ""),
                    "quantity": 0,
                    "average_price": trade.get("average_price"),
                    "last_price": trade.get("last_price"),
                    "unbooked_pnl": trade.get("unbooked_pnl"),
                    "booked_pnl": trade.get("booked_profit_loss"),
                    "instrument_info": trade.get("instrument_info", {}),
                }
            else:
                # Mixed lots can have broker-specific average accounting.
                positions[key]["average_price"] = None
                for target, source in (
                    ("unbooked_pnl", "unbooked_pnl"),
                    ("booked_pnl", "booked_profit_loss"),
                ):
                    left = positions[key][target]
                    right = trade.get(source)
                    positions[key][target] = left + right if left is not None and right is not None else None
            positions[key]["quantity"] += quantity
    for position in positions.values():
        quantity = position["quantity"]
        position["side"] = (
            "SELL" if quantity < 0 else "BUY" if quantity > 0 else "CLOSED"
        )
        position["display_quantity"] = abs(quantity)
    return positions


def _event_type(previous_quantity, new_quantity):
    if previous_quantity == 0:
        return "SHORT_ADDED" if new_quantity < 0 else "LONG_ADDED"
    if new_quantity == 0:
        return "SHORT_CLOSED" if previous_quantity < 0 else "LONG_CLOSED"
    if previous_quantity < 0 and new_quantity < 0:
        return (
            "SHORT_INCREASED"
            if abs(new_quantity) > abs(previous_quantity)
            else "SHORT_REDUCED"
        )
    if previous_quantity > 0 and new_quantity > 0:
        return (
            "LONG_INCREASED"
            if new_quantity > previous_quantity
            else "LONG_REDUCED"
        )
    return "POSITION_REVERSED"


def calculate_position_events(previous_snapshot, current_snapshot):
    previous = normalize_positions(previous_snapshot)
    current = normalize_positions(current_snapshot)
    previous_prices = {
        item.get("trading_symbol", "UNKNOWN"): item.get("underlying_price")
        for item in previous_snapshot.get("data", [])
    }
    current_prices = {
        item.get("trading_symbol", "UNKNOWN"): item.get("underlying_price")
        for item in current_snapshot.get("data", [])
    }
    events = []

    for key in sorted(set(previous) | set(current)):
        old_position = previous.get(key)
        new_position = current.get(key)
        old_quantity = old_position["quantity"] if old_position else 0
        new_quantity = new_position["quantity"] if new_position else 0
        if old_quantity == new_quantity:
            continue

        position = new_position or old_position
        events.append(
            {
                "underlying": position["underlying"],
                "trading_symbol": position["trading_symbol"],
                "product": position["product"],
                "previous_quantity": old_quantity,
                "new_quantity": new_quantity,
                "quantity_delta": new_quantity - old_quantity,
                "event_type": _event_type(old_quantity, new_quantity),
                "previous_underlying_price": previous_prices.get(
                    position["underlying"]
                ),
                "current_underlying_price": current_prices.get(
                    position["underlying"]
                ),
            }
        )
    return events


def _ensure_profile(conn, slug):
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO profiles (slug, name, url) VALUES (?, ?, ?)",
        (
            slug,
            slug,
            "https://web.sensibull.com/verified-pnl/"
            f"{slug}/live-positions?vbs_src=vbs-long-term-pnl",
        ),
    )
    conn.commit()
    return cursor.execute(
        "SELECT id FROM profiles WHERE slug = ?", (slug,)
    ).fetchone()[0]


def _snapshot_time(snapshot):
    source_time = snapshot.get("created_at")
    if source_time:
        try:
            parsed = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("Sensibull created_at has no timezone")
            return parsed.astimezone(IST).replace(tzinfo=None)
        except ValueError as exc:
            raise ValueError(f"Invalid Sensibull created_at: {source_time}") from exc
    raise ValueError("Sensibull snapshot has no created_at timestamp")


def store_position_snapshot(slug, snapshot, detected_at=None):
    source_updated_at = _snapshot_time(snapshot)
    if not isinstance(snapshot.get("data"), list):
        raise ValueError("Sensibull snapshot has no position list")
    detected_at = detected_at or datetime.now(IST).replace(tzinfo=None)
    conn = get_db()
    _configure_connection(conn)
    cursor = conn.cursor()
    profile_id = _ensure_profile(conn, slug)

    previous_row = cursor.execute(
        "SELECT raw_data, timestamp FROM latest_snapshots WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()
    previous_snapshot = json.loads(previous_row["raw_data"]) if previous_row else None
    previous_timestamp = (
        _as_datetime(previous_row["timestamp"]) if previous_row else None
    )
    if previous_timestamp and source_updated_at < previous_timestamp:
        conn.close()
        raise ValueError("Sensibull returned an older snapshot; latest state retained")
    previous_updated_at = (
        _snapshot_time(previous_snapshot)
        if previous_snapshot and previous_snapshot.get("created_at")
        else previous_timestamp
    )
    is_daily_baseline = (
        previous_timestamp is None
        or previous_timestamp.date() != source_updated_at.date()
    )
    events = calculate_position_events(previous_snapshot, snapshot) if previous_snapshot else []

    raw_data = json.dumps(snapshot)
    cursor.execute(
        """
        INSERT INTO live_polls (profile_id, source_updated_at, detected_at, raw_data)
        VALUES (?, ?, ?, ?)
        """,
        (profile_id, source_updated_at, detected_at, raw_data),
    )
    cursor.execute(
        """
        INSERT INTO latest_snapshots (profile_id, raw_data, timestamp)
        VALUES (?, ?, ?)
        ON CONFLICT(profile_id) DO UPDATE SET
            raw_data = excluded.raw_data,
            timestamp = excluded.timestamp
        """,
        (profile_id, raw_data, source_updated_at),
    )

    if is_daily_baseline or events:
        cursor.execute(
            """
            INSERT INTO snapshots (
                profile_id, timestamp, raw_data, created_at_source
            ) VALUES (?, ?, ?, ?)
            """,
            (
                profile_id,
                source_updated_at,
                raw_data,
                snapshot.get("created_at"),
            ),
        )
        snapshot_id = cursor.lastrowid
        summary = (
            "Daily Baseline" if not previous_snapshot else
            f"{len(events)} position change(s)" +
            (" across a session boundary" if is_daily_baseline else "")
        )
        cursor.execute(
            """
            INSERT INTO position_changes (
                snapshot_id, profile_id, timestamp, diff_summary
            ) VALUES (?, ?, ?, ?)
            """,
            (snapshot_id, profile_id, source_updated_at, summary),
        )

        for event in events:
            cursor.execute(
                """
                INSERT OR IGNORE INTO live_position_events (
                    profile_id, snapshot_id, observed_at, previous_updated_at,
                    source_updated_at, detected_at, underlying, trading_symbol,
                    product, previous_quantity, new_quantity, quantity_delta,
                    event_type, previous_underlying_price,
                    current_underlying_price, timing_quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    snapshot_id,
                    detected_at,
                    previous_updated_at,
                    source_updated_at,
                    detected_at,
                    event["underlying"],
                    event["trading_symbol"],
                    event["product"],
                    event["previous_quantity"],
                    event["new_quantity"],
                    event["quantity_delta"],
                    event["event_type"],
                    event["previous_underlying_price"],
                    event["current_underlying_price"],
                    "recorded",
                ),
            )

    conn.commit()
    conn.close()
    return events


def parse_yahoo_candles(payload):
    chart = payload.get("chart", {})
    error = chart.get("error")
    if error:
        raise RuntimeError(f"Yahoo Finance error: {error}")

    results = chart.get("result") or []
    if not results:
        raise RuntimeError("Yahoo Finance returned no chart data")

    result = results[0]
    timestamps = result.get("timestamp") or []
    quote_rows = result.get("indicators", {}).get("quote") or []
    if not quote_rows:
        raise RuntimeError("Yahoo Finance returned no quote data")

    quote = quote_rows[0]
    candles = []
    for index, timestamp in enumerate(timestamps):
        values = {
            "open": quote.get("open", [None] * len(timestamps))[index],
            "high": quote.get("high", [None] * len(timestamps))[index],
            "low": quote.get("low", [None] * len(timestamps))[index],
            "close": quote.get("close", [None] * len(timestamps))[index],
        }
        if any(value is None for value in values.values()):
            continue
        candles.append(
            {
                "timestamp": int(timestamp) // 60 * 60,
                **{key: float(value) for key, value in values.items()},
                "volume": (quote.get("volume") or [None] * len(timestamps))[index],
            }
        )
    return candles


def fetch_nifty_candles():
    response = requests.get(
        YAHOO_NIFTY_URL,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15,
    )
    response.raise_for_status()
    return parse_yahoo_candles(response.json())


def store_candles(candles):
    conn = get_db()
    _configure_connection(conn)
    cursor = conn.cursor()
    cursor.executemany(
        """
        INSERT INTO market_candles (
            symbol, timestamp, open, high, low, close, volume
        ) VALUES ('NIFTY', ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol, timestamp) DO UPDATE SET
            open = excluded.open,
            high = excluded.high,
            low = excluded.low,
            close = excluded.close,
            volume = excluded.volume
        """,
        [
            (
                candle["timestamp"],
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
            )
            for candle in candles
        ],
    )
    conn.commit()
    conn.close()


def _update_status(source, status, message=None, success=False):
    now = datetime.now(IST).replace(tzinfo=None)
    conn = get_db()
    _configure_connection(conn)
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO collector_status (
            source, last_attempt, last_success, status, message
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(source) DO UPDATE SET
            last_attempt = excluded.last_attempt,
            last_success = CASE
                WHEN ? THEN excluded.last_success
                ELSE collector_status.last_success
            END,
            status = excluded.status,
            message = excluded.message
        """,
        (source, now, now if success else None, status, message, success),
    )
    conn.commit()
    conn.close()


class SensibullPageClient:
    def __init__(self, slug):
        self.slug = slug
        self.url = (
            "https://web.sensibull.com/verified-pnl/"
            f"{slug}/live-positions?vbs_src=vbs-long-term-pnl"
        )
        self._playwright = None
        self._browser = None
        self._page = None

    def _start(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is required. Run: pip install -r requirements.txt "
                "and python -m playwright install chromium"
            ) from exc

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=True)
        self._page = self._browser.new_page()

    def collect(self):
        if self._page is None:
            self._start()

        try:
            with self._page.expect_response(
                lambda response: (
                    SENSIBULL_RESPONSE_PATH in response.url
                    and response.status == 200
                ),
                timeout=45_000,
            ) as response_info:
                if self._page.url == "about:blank":
                    self._page.goto(
                        self.url,
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )
                else:
                    self._page.reload(
                        wait_until="domcontentloaded",
                        timeout=45_000,
                    )

            payload = response_info.value.json()
            if not payload.get("success"):
                raise RuntimeError(
                    f"Sensibull returned an unsuccessful response: {payload}"
                )
            snapshot = (
                payload.get("payload", {}).get("position_snapshot_data")
            )
            if not snapshot:
                raise RuntimeError("Sensibull response did not contain a snapshot")
            return snapshot
        except Exception:
            self.close()
            raise

    def close(self):
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._page = None
        self._browser = None
        self._playwright = None


def _collect_positions(client):
    _update_status("sensibull", "running")
    try:
        snapshot = client.collect()
        events = store_position_snapshot(client.slug, snapshot)
        _update_status(
            "sensibull",
            "ok",
            f"Collected snapshot with {len(events)} change(s)",
            success=True,
        )
    except Exception as exc:
        logger.exception("Sensibull collection failed")
        _update_status("sensibull", "error", str(exc))


def _collect_candles():
    _update_status("nifty", "running")
    try:
        candles = fetch_nifty_candles()
        if not candles:
            raise RuntimeError("NIFTY source returned no complete candles")
        store_candles(candles)
        _update_status(
            "nifty",
            "ok",
            f"Stored {len(candles)} candle(s)",
            success=True,
        )
    except Exception as exc:
        logger.exception("NIFTY candle collection failed")
        _update_status("nifty", "error", str(exc))


def run_live_collector(slug=DEFAULT_PROFILE_SLUG, interval_seconds=60):
    init_live_tables()
    client = SensibullPageClient(slug)
    try:
        while True:
            started = time.monotonic()
            _collect_positions(client)
            _collect_candles()
            elapsed = time.monotonic() - started
            time.sleep(max(1, interval_seconds - elapsed))
    finally:
        client.close()


def start_live_collector(slug=DEFAULT_PROFILE_SLUG, interval_seconds=60):
    global _collector_thread
    with _collector_lock:
        if _collector_thread and _collector_thread.is_alive():
            return _collector_thread
        _collector_thread = threading.Thread(
            target=run_live_collector,
            args=(slug, interval_seconds),
            name="live-position-collector",
            daemon=True,
        )
        _collector_thread.start()
        return _collector_thread


def _date_epoch_range(day):
    selected_date = date.fromisoformat(day)
    start = datetime.combine(
        selected_date, datetime_time.min, tzinfo=IST
    )
    end = datetime.combine(
        selected_date, datetime_time.max, tzinfo=IST
    )
    return int(start.timestamp()), int(end.timestamp())


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value)


def _serialize_ist(value):
    if value is None:
        return None
    parsed = _as_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    else:
        parsed = parsed.astimezone(IST)
    return parsed.isoformat()


def get_live_dashboard_data(slug=DEFAULT_PROFILE_SLUG, day=None):
    conn = get_db()
    _configure_connection(conn)
    cursor = conn.cursor()
    profile = cursor.execute(
        "SELECT id FROM profiles WHERE slug = ?", (slug,)
    ).fetchone()

    latest = None
    if profile:
        latest = cursor.execute(
            """
            SELECT raw_data, timestamp
            FROM latest_snapshots
            WHERE profile_id = ?
            """,
            (profile["id"],),
        ).fetchone()

    snapshots = []
    event_rows = []
    polls = []
    if profile:
        snapshots = cursor.execute(
            "SELECT id, raw_data, timestamp FROM snapshots WHERE profile_id = ? ORDER BY id",
            (profile["id"],),
        ).fetchall()
        event_rows = cursor.execute(
            """SELECT * FROM live_position_events WHERE profile_id = ?
               ORDER BY COALESCE(source_updated_at, observed_at), id""",
            (profile["id"],),
        ).fetchall()
        polls = cursor.execute(
            """SELECT source_updated_at, detected_at, raw_data FROM live_polls
               WHERE profile_id = ? ORDER BY id DESC LIMIT 2000""",
            (profile["id"],),
        ).fetchall()
    dates = {_as_datetime(row["timestamp"]).date().isoformat() for row in snapshots}
    if latest:
        dates.add(_as_datetime(latest["timestamp"]).date().isoformat())
    dates = sorted(dates, reverse=True)
    if day is None:
        day = dates[0] if dates else datetime.now(IST).date().isoformat()
    start_epoch, end_epoch = _date_epoch_range(day)

    # Yahoo sometimes returns an additional non-minute quote after market close.
    # Only exchange-session candles define the chart's time axis.
    candle_map = {}
    for row in cursor.execute(
            """
            SELECT timestamp, open, high, low, close
            FROM market_candles
            WHERE symbol = 'NIFTY' AND timestamp BETWEEN ? AND ?
            ORDER BY timestamp
            """,
            (start_epoch, end_epoch),
        ).fetchall():
        timestamp = row["timestamp"] // 60 * 60
        candle_time = datetime.fromtimestamp(timestamp, IST).time()
        if datetime_time(9, 15) <= candle_time < datetime_time(15, 30):
            candle_map[timestamp] = {
                "time": timestamp, "open": row["open"], "high": row["high"],
                "low": row["low"], "close": row["close"],
            }

    snapshot_maps = {
        row["id"]: normalize_positions(json.loads(row["raw_data"]))
        for row in snapshots
    }
    candidates = [
        {"timestamp": row["timestamp"], "raw_data": row["raw_data"]}
        for row in snapshots
    ]
    candidates.extend(
        {"timestamp": row["source_updated_at"], "raw_data": row["raw_data"]}
        for row in polls
    )
    if latest:
        candidates.append(dict(latest))
    candidates = [
        row for row in candidates
        if _as_datetime(row["timestamp"]).date().isoformat() == day
    ]
    selected_snapshot = max(
        candidates, key=lambda row: _as_datetime(row["timestamp"]), default=None
    )
    positions = (
        list(normalize_positions(json.loads(selected_snapshot["raw_data"])).values())
        if selected_snapshot else []
    )
    audit = build_audit(event_rows, snapshot_maps, positions, day)

    status = {
        row["source"]: {
            "last_attempt": _serialize_ist(row["last_attempt"]),
            "last_success": _serialize_ist(row["last_success"]),
            "status": row["status"],
            "message": row["message"],
        }
        for row in cursor.execute(
            "SELECT * FROM collector_status ORDER BY source"
        ).fetchall()
    }
    conn.close()

    return {
        "slug": slug,
        "date": day,
        "dates": dates,
        "last_updated": _serialize_ist(selected_snapshot["timestamp"]) if selected_snapshot else None,
        "latest_source_at": _serialize_ist(latest["timestamp"]) if latest else None,
        "candles": list(candle_map.values()),
        **audit,
        "status": status,
        "polls_recorded": len(polls),
    }


def get_event_evidence(event_id):
    conn = get_db()
    try:
        event = conn.execute(
            "SELECT * FROM live_position_events WHERE id = ?", (event_id,)
        ).fetchone()
        if not event:
            return None
        evidence = {}
        key = (event["trading_symbol"], event["product"])
        for label, stamp in (
            ("before", event["previous_updated_at"]),
            ("after", event["source_updated_at"]),
        ):
            poll = conn.execute(
                """SELECT raw_data, detected_at FROM live_polls
                   WHERE profile_id = ? AND source_updated_at = ?
                   ORDER BY id LIMIT 1""",
                (event["profile_id"], stamp),
            ).fetchone()
            position = (
                normalize_positions(json.loads(poll["raw_data"])).get(key)
                if poll else None
            )
            evidence[label] = {
                "source_updated_at": _serialize_ist(stamp),
                "poll_detected_at": _serialize_ist(poll["detected_at"]) if poll else None,
                "poll_available": poll is not None,
                "row_present": position is not None if poll else None,
                "quantity": position["quantity"] if position else (0 if poll else None),
                "position": position,
            }
        evidence["event_id"] = event_id
        evidence["trading_symbol"] = event["trading_symbol"]
        evidence["first_detected_at"] = (
            _serialize_ist(event["detected_at"])
            if event["timing_quality"] == "recorded" else None
        )
        evidence["quantity_verified"] = (
            evidence["before"]["quantity"] == event["previous_quantity"]
            and evidence["after"]["quantity"] == event["new_quantity"]
        ) if all(evidence[side]["poll_available"] for side in ("before", "after")) else None
        return evidence
    finally:
        conn.close()
