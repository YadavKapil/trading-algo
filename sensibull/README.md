# Sensibull trade audit

A local, read-only Flask dashboard for tracing changes in a public Sensibull
portfolio. One Python process, one SQLite database; no broker connection or orders.

## Run

From the `sensibull` directory:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
python app.py
```

Open <http://localhost:6060>. `/live` is the same dashboard; `/tracker` retains the
older multi-profile history. Keep the process running to collect observations.
It binds to localhost only. A second collector must not write the same profile.
The obsolete scraper-monitor thread is not started.

Optional environment variables:

- `SENSIBULL_LIVE_SLUG`: public profile, default `latered-garage`.
- `SENSIBULL_DB_PATH`: SQLite file, default `sensibull.db` in this directory.

The public page is read in a fresh anonymous Playwright browser context. There is
no broker login, token extraction, or authentication bypass. Use only permitted
public pages and respect the source's terms and rate limits.

## Audit workflow

1. Select the session (defaults to the most recent source session), underlying,
   expiry, or instrument. Historical positions come from that date, not today's
   final positions.
2. Read the chronological **buy / sell ledger**. Positive net quantity movement is
   a net BUY; negative movement is a net SELL. Before/after signed inventory,
   quantity, prior source update, new source update, first local detection, spot
   observations, and observed option quote remain separate.
   The header separates source-update batches from instrument-level net changes.
   Each Evidence button opens the saved before/after poll quantities and timestamps.
   Source updates outside market hours are flagged, not treated as proof of
   an execution at that time.
3. Use **FIFO entry / exit tracing** to match opposite quantity changes. Partial
   exits split the quantity; reversals close one side and open the remainder.
   Baseline inventory has unknown entry time. FIFO is a transparent accounting
   assumption, not the author's actual allocation or a broker execution book.
   A single change can produce multiple FIFO allocation rows. Event IDs link the
   opening and reducing changes; allocation rows are not additional trades.
4. Click an instrument to filter all tables, or a detection time to focus the chart.
   Export the filtered ledger as CSV. Use **Fit session** to reset chart navigation.

The collector polls every minute; the browser reads the local API every five
seconds. New successful polls retain raw data and local detection time in
`live_polls`, including unchanged polls. Change snapshots, normalized events, and
the latest state use the existing database. Existing records are preserved during
additive schema migration. Legacy source timestamps are not claimed as actual
local detection times. Unknown times remain unknown.

All displayed times are IST. First detection is the first recorded net change in
a position lifecycle, not necessarily its original entry. An opening or reversal
starts a lifecycle; a partial reduction or closure does not overwrite its start.
Positions already present when observation began are labeled baseline.

## Chart

TradingView **Lightweight Charts** is loaded from a pinned CDN version and uses
public Yahoo Finance `^NSEI` one-minute candles. This is not a guaranteed real-time
or exchange-authorized execution feed. Only regular-session, minute-aligned
candles define the chart axis. Non-minute after-hours quotes are excluded.

Green dots are net option buys; red dots are net option sells. Quantities in a
minute may aggregate several instruments; filter one instrument for a clear trace.
Detection windows are canvas overlays from the previous source update to local
detection, between observed spot values. They are not actual price paths or fill
prices, and cannot push the chart's time axis. Markers use the containing candle;
precise timestamps remain in the ledger. Changes outside captured candles remain
in the ledger without invented chart prices. Strike lines are optional and off
by default. Refresh preserves the viewport.

## Evidence limits and strategy research

The page exposes **net inventory**, not complete executions. Multiple fills can
net together. A buy and sell between polls can cancel out completely, even while
turnover occurred. A vanished position can be a close, expiry, or source omission;
a BUY/SELL label is an inferred net action, not proof of an order.

Prior source update, new source update and local first detection are distinct.
The displayed interval is an observation window; broker-to-source latency is
unknown, so it is not a guaranteed execution-time bound. Across-session changes
are recorded with wide-gap warnings rather than silently discarded.

The instance summary compares PE/CE sells with movement between source snapshots.
This is descriptive only, not a test of Survivor's hidden reference/reset state.
Treat each underlying and expiry as a separate possible instance; do not assume
checked-in default quantities, gaps or symbols. Short-option inventory alone
cannot distinguish Survivor from Wave, manual actions or other management.

**Replicability and profitable delayed signal-following are not established.**
Evaluate out of sample in paper replay at first detection plus several delays.
That requires independent historical option bid/ask data, fees, slippage,
liquidity, margin, partial fills and complete hedge legs. Reported average prices
can include broker-specific adjustments; observed LTP is not an executable price.
This app deliberately does not invent execution P&L or automatically trade signals.

## Checks

```powershell
python -m unittest test_live_dashboard.py
```

Tests cover storage and time provenance, overnight/date selection, minute-axis
normalization, stale payload rejection, FIFO matching, partial exits, baseline
inventory and reversals. `verify_diff.py` remains the older tracker's snapshot
diff diagnostic; reconciling inventory does not imply every fill was observed.
