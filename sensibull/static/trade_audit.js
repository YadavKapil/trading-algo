/* The candle series alone owns the time axis. Event windows are canvas overlays. */
const element = id => document.getElementById(id);
const escapeText = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
}[char]));
const number = (value, digits = 0) => value === null || value === undefined ? 'Unknown' :
    Number(value).toLocaleString('en-IN', { maximumFractionDigits: digits });
const timeText = value => value ? new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
}).format(new Date(value)) : 'Unknown';
const axisTime = timestamp => new Intl.DateTimeFormat('en-IN', {
    timeZone: 'Asia/Kolkata', hour: '2-digit', minute: '2-digit', hour12: false
}).format(new Date(timestamp * 1000));
const duration = value => value === null || value === undefined ? 'Unknown' : `${number(value, 1)}s`;

let payload = null;
let requestedDay = '';
let renderedDay = '';
let candleSignature = '';
let candles = [];
let drawnEvents = [];
let strikeLines = [];
let focusedEvent = null;
let requestInFlight = false;
let redrawPending = false;

if (typeof LightweightCharts === 'undefined') {
    element('request-error').textContent =
        'The TradingView chart library could not be loaded from the CDN. Check network access and reload.';
    element('request-error').style.display = 'block';
    throw new Error('Chart library unavailable');
}
const chart = LightweightCharts.createChart(element('chart'), {
    autoSize: true,
    layout: { background: { color: '#171b22' }, textColor: '#c9d1d9' },
    grid: { vertLines: { color: '#252b33' }, horzLines: { color: '#252b33' } },
    localization: { timeFormatter: axisTime },
    timeScale: {
        timeVisible: true, secondsVisible: false, tickMarkFormatter: axisTime,
        shiftVisibleRangeOnNewBar: false, rightOffset: 3
    },
    rightPriceScale: { borderColor: '#30363d' }
});
const series = chart.addCandlestickSeries({
    upColor: '#26a69a', downColor: '#ef5350', borderVisible: false,
    wickUpColor: '#26a69a', wickDownColor: '#ef5350'
});

function filterOptions() {
    const underlying = element('underlying').value;
    const expiry = element('expiry').value;
    const search = element('instrument').value.trim().toUpperCase();
    return item => {
        const instrument = item.entry || item.exit || item;
        const symbol = item.trading_symbol || '';
        const metadata = payload.positions.find(p => p.trading_symbol === symbol);
        const itemExpiry = instrument.expiry || metadata?.instrument_info?.expiry;
        return (!underlying || item.underlying === underlying) &&
            (!expiry || itemExpiry === expiry) &&
            (!search || symbol.toUpperCase().includes(search));
    };
}

function updateOptions(select, values, firstLabel) {
    const previous = select.value;
    const options = ['', ...values];
    select.replaceChildren(...options.map(value => {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = value || firstLabel;
        return option;
    }));
    select.value = options.includes(previous) ? previous : '';
}

function chooseInstrument(symbol) {
    element('instrument').value = symbol;
    render();
}

function symbolButton(symbol) {
    return `<button class="link instrument-link" data-symbol="${escapeText(symbol)}">${escapeText(symbol)}</button>`;
}

function tableRows(id, rows, columns) {
    element(id).innerHTML = rows.length ? rows.join('') :
        `<tr><td colspan="${columns}" class="muted">No matching records in the captured data.</td></tr>`;
}

function renderTables(events, positions, matches) {
    element('event-count').textContent = `(${events.length})`;
    tableRows('events', events.slice().reverse().map(event => `
        <tr class="${event.id === focusedEvent ? 'selected' : ''}">
        <td>${timeText(event.previous_updated_at)}</td>
        <td><button class="link event-link" data-id="${event.id}">${timeText(event.detected_at)}</button></td>
        <td>${symbolButton(event.trading_symbol)}</td>
        <td class="${event.action.toLowerCase()}">${event.action} (net)</td>
        <td class="numeric">${number(event.quantity)}</td>
        <td class="numeric">${number(event.previous_quantity)} / ${number(event.new_quantity)}</td>
        <td>${escapeText(event.event_type.replaceAll('_', ' '))}</td>
        <td>${timeText(event.source_updated_at)}</td>
        <td>${duration(event.window_seconds)}</td>
        <td>${number(event.previous_underlying_price, 2)} / ${number(event.current_underlying_price, 2)}</td>
        <td>${number(event.option_quote, 2)}</td>
        <td class="${event.gap || event.after_hours ? 'warning' : 'muted'}">
        <button class="link evidence-link" data-id="${event.id}">Evidence #${event.id}</button><br>
        ${event.timing_quality === 'recorded' ? 'Local detection recorded' : 'Legacy source time only'}${event.gap ? '; wide gap' : ''}
        ${event.after_hours ? '; source update outside market hours: not confirmed execution' : ''}</td>
        </tr>`), 12);

    tableRows('matches', matches.map(match => `<tr>
        <td>${symbolButton(match.trading_symbol)}</td><td>${match.direction}</td>
        <td class="numeric">${number(match.quantity)}</td>
        <td>${timeText(match.entry?.detected_at)}${match.entry ? ` (change #${match.entry.id})` : ''}</td>
        <td>${timeText(match.exit?.detected_at)}${match.exit ? ` (change #${match.exit.id})` : ''}</td>
        <td>${match.entry?.detected_at && match.exit?.detected_at ?
            duration((new Date(match.exit.detected_at) - new Date(match.entry.detected_at)) / 1000) : 'Unknown'}</td>
        <td>${escapeText(match.state)}</td></tr>`), 7);

    element('position-count').textContent = `(${positions.length})`;
    tableRows('positions', positions.map(position => {
        const pnl = position.unbooked_pnl === null || position.booked_pnl === null ? null :
            position.unbooked_pnl + position.booked_pnl;
        const evidence = position.timing_quality === 'baseline' ? 'Present before observed change history' :
            position.timing_quality === 'source_time_only' ? 'Legacy: local detection unknown' : 'Recorded net changes';
        return `<tr><td>${symbolButton(position.trading_symbol)}</td><td>${position.side}</td>
            <td class="numeric">${number(position.display_quantity)}</td>
            <td>${number(position.average_price, 2)}</td><td>${number(position.last_price, 2)}</td>
            <td class="${pnl < 0 ? 'sell' : 'buy'}">${number(pnl, 2)}</td>
            <td>${timeText(position.previous_updated_at)}</td>
            <td>${timeText(position.first_detected_at)}</td><td>${timeText(position.last_changed_at)}</td>
            <td class="muted">${evidence}</td></tr>`;
    }), 10);
    element('snapshot-time').textContent = `Source snapshot: ${timeText(payload.last_updated)}. ` +
        'First detected means first observed net change in this lifecycle, not an exact broker entry time.';
}

function renderHealth() {
    const now = Date.now();
    const lines = ['sensibull', 'nifty'].map(source => {
        const status = payload.status[source];
        if (!status) return `${source}: not started`;
        const stale = !status.last_success || now - new Date(status.last_success).getTime() > 180000;
        const css = status.status === 'error' ? 'error' : stale ? 'warning' : 'buy';
        return `<span class="${css}">${source}: ${escapeText(status.status)}${stale ? ' (collector stale)' : ''}</span>` +
            ` | polled ${timeText(status.last_success)}` +
            (status.status === 'error' ? `<br>${escapeText(status.message)}` : '');
    });
    const age = payload.latest_source_at ? Math.max(0, (now - new Date(payload.latest_source_at).getTime()) / 1000) : null;
    lines.push(`Latest source update: ${timeText(payload.latest_source_at)} | age ${duration(age)}`);
    element('status').innerHTML = lines.join('<br>');
}

function renderResearch(events, matches) {
    const timed = events.map(e => e.window_seconds).filter(value => value !== null).sort((a, b) => a - b);
    const median = timed.length ? (timed[Math.floor((timed.length - 1) / 2)] + timed[Math.floor(timed.length / 2)]) / 2 : null;
    const matched = matches.filter(m => m.state === 'MATCHED').reduce((total, m) => total + m.quantity, 0);
    element('cards').innerHTML = [
        ['Source updates / net changes', `${new Set(events.map(e => e.source_updated_at)).size} / ${events.length}`],
        ['BUY / SELL qty (unweighted)', `${number(events.filter(e => e.action === 'BUY').reduce((n, e) => n + e.quantity, 0))} / ${number(events.filter(e => e.action === 'SELL').reduce((n, e) => n + e.quantity, 0))}`],
        ['Median detection window', duration(median)],
        ['FIFO matched quantity', number(matched)]
    ].map(([label, value]) => `<div class="card"><span class="muted">${label}</span><strong>${value}</strong></div>`).join('');
    const gaps = events.filter(e => e.gap).length;
    const baseline = matches.filter(m => m.state.startsWith('BASELINE')).length;
    element('research').textContent =
        `Selected session: ${payload.date}. ${events.length} net changes; ${gaps} windows longer than 180 seconds; ` +
        `${baseline} FIFO rows with an unknown baseline entry; ${payload.discontinuities.length} inventory discontinuities in captured history. ` +
        'Use expiry and instrument filters to compare entry sequences, add-ons and exits. ' +
        'The measured window includes source-update delay and polling delay; it is not a guaranteed bound on broker execution time. ' +
        'Replicability and profitable delayed execution are not established by these observations.';
    const instances = new Map();
    for (const event of events) {
        const key = `${event.underlying} / ${event.expiry || 'unknown expiry'}`;
        if (!instances.has(key)) instances.set(key, { pe: 0, ce: 0, buy: 0, aligned: 0, measured: 0 });
        const row = instances.get(key);
        const type = String(event.option_type || '').toUpperCase();
        const put = type === 'PE' || type === 'PUT';
        const call = type === 'CE' || type === 'CALL';
        if (event.action === 'BUY') row.buy += event.quantity;
        if (event.action === 'SELL') {
            if (put) row.pe += event.quantity;
            if (call) row.ce += event.quantity;
            if ((put || call) && event.previous_underlying_price !== null && event.current_underlying_price !== null) {
                row.measured++;
                const change = event.current_underlying_price - event.previous_underlying_price;
                if ((put && change > 0) || (call && change < 0)) row.aligned++;
            }
        }
    }
    tableRows('instances', [...instances].map(([key, row]) =>
        `<tr><td>${escapeText(key)}</td><td>${number(row.pe)}</td><td>${number(row.ce)}</td>` +
        `<td>${number(row.buy)}</td><td>${row.aligned} / ${row.measured} observed sell changes</td></tr>`), 5);
}

function eventBucket(event) {
    if (!event.detected_at) return null;
    const minute = Math.floor(event.time / 60) * 60;
    return candles.some(candle => candle.time === minute) ? minute : null;
}

function renderChart(events, positions) {
    const signature = JSON.stringify(payload.candles);
    const dateChanged = renderedDay !== payload.date;
    const viewport = !dateChanged ? chart.timeScale().getVisibleLogicalRange() : null;
    if (signature !== candleSignature) {
        candles = payload.candles;
        series.setData(candles);
        candleSignature = signature;
    }
    const chartEvents = events.filter(event => event.underlying === 'NIFTY');
    const groups = new Map();
    for (const event of chartEvents) {
        const time = eventBucket(event);
        if (time === null) continue;
        const key = `${time}-${event.action}`;
        if (!groups.has(key)) groups.set(key, { time, action: event.action, quantity: 0, count: 0 });
        const group = groups.get(key);
        group.quantity += event.quantity;
        group.count += 1;
    }
    series.setMarkers([...groups.values()].sort((a, b) => a.time - b.time).map(group => ({
        time: group.time, position: group.action === 'BUY' ? 'belowBar' : 'aboveBar',
        color: group.action === 'BUY' ? '#67dbab' : '#ff8e88',
        shape: 'circle', text: element('show-labels').checked || element('instrument').value ?
            `${group.action} ${group.quantity} (${group.count})` : ''
    })));
    strikeLines.forEach(line => series.removePriceLine(line));
    strikeLines = [];
    if (element('show-strikes').checked) {
        for (const position of positions) {
            const info = position.instrument_info || {};
            if (position.underlying !== 'NIFTY' || !position.quantity || !info.strike) continue;
            strikeLines.push(series.createPriceLine({
                price: Number(info.strike), color: '#718096',
                lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true,
                title: `${position.side} ${position.display_quantity} | ${position.trading_symbol}`
            }));
        }
    }
    drawnEvents = chartEvents;
    if (dateChanged && candles.length) chart.timeScale().fitContent();
    else if (viewport) chart.timeScale().setVisibleLogicalRange(viewport);
    renderedDay = payload.date;
    element('chart-note').textContent = candles.length ?
        `${payload.date} | ${candles.length} regular-session candles. ` +
        `${chartEvents.filter(e => eventBucket(e) === null).length} changes without a recorded detection time or outside available candles are in the ledger only.` :
        'No NIFTY candles were captured for this session. The ledger remains available; no prices are invented.';
    scheduleDraw();
}

function coordinateAt(timestamp) {
    if (!candles.length) return null;
    if (timestamp <= candles[0].time) return chart.timeScale().timeToCoordinate(candles[0].time);
    if (timestamp >= candles[candles.length - 1].time) {
        return chart.timeScale().timeToCoordinate(candles[candles.length - 1].time);
    }
    let low = 0;
    let high = candles.length - 1;
    while (high - low > 1) {
        const middle = Math.floor((low + high) / 2);
        if (candles[middle].time <= timestamp) low = middle;
        else high = middle;
    }
    const left = chart.timeScale().timeToCoordinate(candles[low].time);
    const right = chart.timeScale().timeToCoordinate(candles[high].time);
    if (left === null || right === null) return null;
    return left + (right - left) * (timestamp - candles[low].time) /
        (candles[high].time - candles[low].time);
}

function drawWindows() {
    redrawPending = false;
    const canvas = element('windows');
    const bounds = element('chart-wrap').getBoundingClientRect();
    const ratio = window.devicePixelRatio || 1;
    canvas.width = bounds.width * ratio;
    canvas.height = bounds.height * ratio;
    canvas.style.width = `${bounds.width}px`;
    canvas.style.height = `${bounds.height}px`;
    const context = canvas.getContext('2d');
    context.scale(ratio, ratio);
    context.clearRect(0, 0, bounds.width, bounds.height);
    if (!element('show-windows').checked || !candles.length) return;
    const endOfCandles = candles[candles.length - 1].time + 60;
    const seen = new Set();
    for (const event of drawnEvents) {
        if (!event.detected_at || event.previous_time === null || event.time <= event.previous_time ||
            event.current_underlying_price === null || event.previous_underlying_price === null ||
            event.previous_time > endOfCandles || event.time < candles[0].time) continue;
        const key = `${event.previous_time}-${event.time}-${event.action}`;
        if (seen.has(key) && event.id !== focusedEvent) continue;
        seen.add(key);
        const x1 = coordinateAt(event.previous_time);
        const x2 = coordinateAt(event.time);
        const y1 = series.priceToCoordinate(event.previous_underlying_price);
        const y2 = series.priceToCoordinate(event.current_underlying_price);
        if ([x1, x2, y1, y2].some(value => value === null)) continue;
        context.strokeStyle = event.action === 'BUY' ? '#67dbab' : '#ff8e88';
        context.lineWidth = event.id === focusedEvent ? 4 : 2;
        context.globalAlpha = focusedEvent && event.id !== focusedEvent ? 0.25 : 0.8;
        context.beginPath();
        context.moveTo(x1, y1);
        context.lineTo(x2, y2);
        context.stroke();
    }
    context.globalAlpha = 1;
}

function scheduleDraw() {
    if (!redrawPending) {
        redrawPending = true;
        requestAnimationFrame(drawWindows);
    }
}

function render() {
    if (!payload) return;
    const predicate = filterOptions();
    const events = payload.events.filter(predicate);
    const positions = payload.positions.filter(predicate);
    const matches = payload.matches.filter(predicate);
    renderTables(events, positions, matches);
    renderResearch(events, matches);
    renderHealth();
    renderChart(events, positions);
}

async function refresh() {
    if (requestInFlight) return;
    requestInFlight = true;
    try {
        const params = new URLSearchParams({ slug });
        if (requestedDay) params.set('date', requestedDay);
        const response = await fetch(`/api/live?${params}`, { cache: 'no-store' });
        if (!response.ok) throw new Error(`Dashboard API returned HTTP ${response.status}`);
        const data = await response.json();
        if (data.error) throw new Error(data.error);
        payload = data;
        updateOptions(element('session'), data.dates, 'Latest available');
        element('session').value = requestedDay;
        const underlyingBefore = element('underlying').value;
        const underlyings = [...new Set([...data.positions, ...data.events].map(p => p.underlying))].sort();
        updateOptions(element('underlying'), underlyings, 'All');
        if (underlyings.includes(underlyingBefore)) element('underlying').value = underlyingBefore;
        const expiries = [...new Set(data.positions.map(p => p.instrument_info?.expiry).filter(Boolean))].sort();
        updateOptions(element('expiry'), expiries, 'All');
        element('request-error').style.display = 'none';
        render();
    } catch (error) {
        element('request-error').textContent = `Update failed: ${error.message}. Last successful view retained.`;
        element('request-error').style.display = 'block';
    } finally {
        requestInFlight = false;
    }
}

element('session').addEventListener('change', () => {
    requestedDay = element('session').value;
    focusedEvent = null;
    refresh();
});
element('latest').onclick = () => { requestedDay = ''; focusedEvent = null; refresh(); };
['underlying', 'expiry', 'show-strikes', 'show-windows', 'show-labels'].forEach(id => element(id).addEventListener('change', render));
element('instrument').addEventListener('input', render);
element('clear').onclick = () => {
    element('underlying').value = 'NIFTY';
    element('expiry').value = '';
    element('instrument').value = '';
    focusedEvent = null;
    render();
};
element('fit').onclick = () => { chart.timeScale().fitContent(); scheduleDraw(); };
document.addEventListener('click', event => {
    const evidence = event.target.closest('.evidence-link');
    if (evidence) {
        element('evidence-panel').open = true;
        element('evidence').textContent = 'Loading saved before/after polls...';
        fetch(`/api/live/evidence/${evidence.dataset.id}`)
            .then(response => {
                if (!response.ok) throw new Error(`Evidence API: HTTP ${response.status}`);
                return response.json();
            })
            .then(data => { element('evidence').textContent = JSON.stringify(data, null, 2); })
            .catch(error => { element('evidence').textContent = error.message; });
        return;
    }
    const instrument = event.target.closest('.instrument-link');
    if (instrument) chooseInstrument(instrument.dataset.symbol);
    const target = event.target.closest('.event-link');
    if (!target || !payload) return;
    const item = payload.events.find(row => row.id === Number(target.dataset.id));
    if (!item) return;
    focusedEvent = item.id;
    if (item.underlying === 'NIFTY' && eventBucket(item) !== null) {
        const start = item.previous_time || item.time;
        chart.timeScale().setVisibleRange({ from: start - 300, to: item.time + 300 });
        element('chart-wrap').scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    render();
});
element('export').onclick = () => {
    if (!payload) return;
    const columns = ['previous_updated_at', 'source_updated_at', 'detected_at', 'underlying',
        'trading_symbol', 'product', 'action', 'quantity', 'previous_quantity', 'new_quantity',
        'window_seconds', 'option_quote', 'timing_quality'];
    const csv = [columns, ...payload.events.filter(filterOptions()).map(event => columns.map(key => event[key]))]
        .map(row => row.map(value => `"${String(value ?? '').replaceAll('"', '""')}"`).join(',')).join('\r\n');
    const url = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `trade-audit-${payload.date}.csv`;
    anchor.click();
    URL.revokeObjectURL(url);
};
chart.timeScale().subscribeVisibleLogicalRangeChange(scheduleDraw);
chart.subscribeCrosshairMove(scheduleDraw);
new ResizeObserver(scheduleDraw).observe(element('chart-wrap'));
element('chart-wrap').addEventListener('wheel', scheduleDraw);
refresh();
setInterval(refresh, 5000);
