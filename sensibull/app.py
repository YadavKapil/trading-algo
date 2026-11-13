from flask import Flask, render_template, request, jsonify, redirect, url_for
import sqlite3
import json
import logging
from datetime import datetime, timedelta
from database import get_db, sync_profiles
from live_dashboard import (
    DEFAULT_PROFILE_SLUG,
    get_live_dashboard_data,
    get_event_evidence,
    init_live_tables,
    start_live_collector,
)
import sys
import os
import threading
import time
import subprocess

app = Flask(__name__)
init_live_tables()
# Configure standard port or 5010 as per previous context
PORT = 6060

# Global variable to track last restart to prevent loops
LAST_AUTO_RESTART = None

@app.template_filter('to_datetime')
def to_datetime_filter(value):
    # Timestamps in DB are now consistently Naive IST (from scraper datetime.now())
    # So we do NOT add any offset. We just ensure it's a datetime object.
    if isinstance(value, datetime):
        return value
    
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    except ValueError:
        try:
             # Handle ISO format
             dt = datetime.fromisoformat(value)
             return dt.replace(tzinfo=None) # Ensure naive
        except:
             return value

def is_market_open():
    now = datetime.now()
    # Weekday check: 0=Monday, 4=Friday, 5=Saturday, 6=Sunday
    if now.weekday() > 4:
        return False
    
    # Time check: 09:15 to 15:30
    current_time = now.time()
    start_time = datetime.strptime("09:15", "%H:%M").time()
    end_time = datetime.strptime("15:30", "%H:%M").time()
    
    return start_time <= current_time <= end_time

def restart_scraper_internal():
    print("Restarting scraper process...")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    scraper_path = os.path.join(base_dir, 'scraper.py')
    log_file = os.path.join(base_dir, 'scraper.log')
    
    # Kill existing
    os.system("pkill -f 'vibhu/sensibull/scraper.py'")
    
    # Start new
    cmd = f"nohup python3 {scraper_path} >> {log_file} 2>&1 &"
    os.system(cmd)
    
def monitor_scraper():
    """Background thread to monitor scraper and restart if stuck"""
    global LAST_AUTO_RESTART
    print("Scraper monitor thread started.")
    
    while True:
        try:
            # Check every minute
            time.sleep(60)
            
            if not is_market_open():
                continue
                
            # Check DB for last update
            conn = get_db()
            c = conn.cursor()
            last_updated_row = c.execute("SELECT MAX(timestamp) FROM latest_snapshots").fetchone()
            conn.close()
            
            last_updated = last_updated_row[0] if last_updated_row else None
            
            should_restart = False
            
            if last_updated:
                last_dt = to_datetime_filter(last_updated)
                time_diff = (datetime.now() - last_dt).total_seconds()
                
                # If no update for > 5 minutes (300 seconds)
                if time_diff > 300:
                    print(f"Monitor: Scraper stuck! Last update {time_diff}s ago.")
                    should_restart = True
            else:
                 # If no data at all and market is open, maybe assume stuck? 
                 # Or maybe database is empty. Let's be conservative and only restart if data exists but is stale,
                 # or perhaps if empty database persists for long. 
                 # For now, only restart if stale.
                 pass
                 
            if should_restart:
                # Check cooldown (don't restart if we just restarted < 5 mins ago)
                if LAST_AUTO_RESTART and (datetime.now() - LAST_AUTO_RESTART).total_seconds() < 300:
                    print("Monitor: Skipping restart due to cooldown.")
                else:
                    print("Monitor: Triggering auto-restart.")
                    restart_scraper_internal()
                    LAST_AUTO_RESTART = datetime.now()
                    
        except Exception as e:
            print(f"Monitor error: {e}")
            # Sleep a bit to avoid rapid loops on error
            time.sleep(10)

@app.route('/')
def index():
    return redirect(url_for('live_dashboard'))


@app.route('/tracker')
def tracker_dashboard():
    # Sync profiles from file on every refresh
    sync_profiles()
    
    conn = get_db()
    c = conn.cursor()
    
    # Get all profiles
    profiles_db = c.execute("SELECT * FROM profiles").fetchall()
    
    # Get order from urls.txt
    ordered_slugs = []
    try:
        urls_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'urls.txt')
        if os.path.exists(urls_path):
            with open(urls_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        slug = line.split('/')[-1] if 'sensibull.com' in line else line
                        ordered_slugs.append(slug)
    except Exception as e:
        print(f"Error reading urls.txt for sorting: {e}")

    # Create a map for sorting
    # profile_key -> index
    # We use a large number if not found so they go to the bottom
    sort_map = {slug: i for i, slug in enumerate(ordered_slugs)}
    
    # Sort the DB profiles: those in urls.txt first (in order), others after
    profiles = sorted(profiles_db, key=lambda p: sort_map.get(p['slug'], 99999))

    # Calculate dates (last 30 days)
    
    # We have profiles now. Now get dates.
    # Get unique dates from changes
    dates_rows = c.execute("SELECT DISTINCT date(timestamp) as day FROM position_changes ORDER BY day DESC LIMIT 30").fetchall()
    dates = [row['day'] for row in dates_rows]
    
    # Build matrix
    matrix = {} 
    for p in profiles:
        for d in dates:
            # Check if any changes on this day
            count = c.execute("""
                SELECT COUNT(*) FROM position_changes 
                WHERE profile_id = ? AND date(timestamp) = ?
            """, (p['id'], d)).fetchone()[0]
            
            pnl = 0
            if count > 0:
                 metrics = get_daily_pnl_metrics(c, p['id'], d)
                 pnl = metrics['todays_pnl']
                 
            matrix[(p['id'], d)] = {'count': count, 'pnl': pnl}
            
    # Get global last updated time
    last_updated_row = c.execute("SELECT MAX(timestamp) FROM latest_snapshots").fetchone()
    last_updated = last_updated_row[0] if last_updated_row else None
    
    scraper_error = None
    if not is_market_open():
        scraper_error = "Scraper is paused (Market Closed)"
    else:
        # Check if stuck (no update in last 3 minutes)
        if last_updated:
            last_dt = to_datetime_filter(last_updated)
            if (datetime.now() - last_dt).total_seconds() > 180:
                scraper_error = "Scraper is stuck or not running! (Last update > 3 mins ago)"
        else:
             scraper_error = "Scraper has no data yet!"

    conn.close()
    return render_template('index.html', profiles=profiles, dates=dates, matrix=matrix, last_updated=last_updated, scraper_error=scraper_error)


@app.route('/live')
def live_dashboard():
    return render_template(
        'live_dashboard.html',
        slug=request.args.get('slug', DEFAULT_PROFILE_SLUG),
    )


@app.route('/api/live')
def api_live_dashboard():
    slug = request.args.get('slug', DEFAULT_PROFILE_SLUG)
    day = request.args.get('date')
    try:
        return jsonify(get_live_dashboard_data(slug=slug, day=day))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400


@app.route('/api/live/evidence/<int:event_id>')
def api_live_evidence(event_id):
    evidence = get_event_evidence(event_id)
    if evidence is None:
        return jsonify({'error': 'Event not found'}), 404
    return jsonify(evidence)

def calculate_snapshot_pnl(c, snapshot_id):
    snap = c.execute("SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if not snap: return 0, 0
    raw = json.loads(snap['raw_data'])
    data = raw.get('data', [])
    
    # Calculate manually to be safe
    total = 0
    booked = 0
    
    for item in data:
        for trade in item.get('trades', []):
            u_pnl = trade.get('unbooked_pnl', 0)
            b_pnl = trade.get('booked_profit_loss', 0)
            
            total += (u_pnl + b_pnl)
            booked += b_pnl
            
    return total, booked

def get_daily_pnl_metrics(c, profile_id, date):
    # 1. Start Day P&L
    start_day_pnl = 0
    
    # Try to get previous day's close
    prev_change = c.execute("""
        SELECT * FROM position_changes 
        WHERE profile_id = ? AND date(timestamp) < ? 
        ORDER BY timestamp DESC LIMIT 1
    """, (profile_id, date)).fetchone()
    
    if prev_change:
        prev_total, prev_booked = calculate_snapshot_pnl(c, prev_change['snapshot_id'])
        start_day_pnl = prev_total - prev_booked
    else:
        # Fallback to first change of the day
        first_change = c.execute("""
            SELECT * FROM position_changes 
            WHERE profile_id = ? AND date(timestamp) = ? 
            ORDER BY timestamp ASC LIMIT 1
        """, (profile_id, date)).fetchone()
        
        if first_change:
            total, booked = calculate_snapshot_pnl(c, first_change['snapshot_id'])
            # Only count unbooked if it's the very first record? 
            # Or use total? If we fallback, it's safer to assume 0 start or use total.
            # Let's stick to total.
            start_day_pnl = total
            
    # 2. Current P&L (Latest available snapshot for the day)
    # We query the `latest_snapshots` table which is updated on every scraper run
    # regardless of position changes. This gives us Realtime P&L.
    
    current_pnl = 0
    booked_pnl = 0
    
    # First try latest_snapshots for realtime data
    latest_realtime = c.execute("SELECT * FROM latest_snapshots WHERE profile_id = ?", (profile_id,)).fetchone()
    
    # We only use realtime if it matches the requested date
    # (Or should we always use it if date is TODAY? Yes.)
    # If date is in the past, we must fall back to historical snapshots.
    
    today_str = datetime.now().strftime('%Y-%m-%d')
    use_realtime = (date == today_str) and (latest_realtime is not None)
    
    last_updated = None
    
    if use_realtime:
        # Parse raw_data manually since we don't have calculate_snapshot_pnl helper for raw JSON input
        raw = json.loads(latest_realtime['raw_data'])
        last_updated = latest_realtime['timestamp'] # Get timestamp from latest_snapshots
        data = raw.get('data', [])
        total = 0
        booked = 0
        for item in data:
            for trade in item.get('trades', []):
                total += (trade.get('unbooked_pnl', 0) + trade.get('booked_profit_loss', 0))
                booked += trade.get('booked_profit_loss', 0)
        current_pnl = total
        booked_pnl = booked
    else:
        # Fallback to history (Last recorded snapshot for that day)
        latest_snapshot = c.execute("""
            SELECT * FROM snapshots 
            WHERE profile_id = ? AND date(timestamp) = ? 
            ORDER BY timestamp DESC LIMIT 1
        """, (profile_id, date)).fetchone()
        
        if latest_snapshot:
            current_pnl, booked_pnl = calculate_snapshot_pnl(c, latest_snapshot['id'])
            last_updated = latest_snapshot['timestamp']
    
    todays_pnl = current_pnl - start_day_pnl
    
    return {
        'start_pnl': start_day_pnl,
        'current_pnl': current_pnl,
        'todays_pnl': todays_pnl,
        'booked_pnl': booked_pnl,
        'last_updated': last_updated
    }

@app.route('/profile/<slug>/<date>')
def daily_view(slug, date):
    conn = get_db()
    c = conn.cursor()
    
    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return "Profile not found", 404
    
    # Get changes for this date
    changes = c.execute("""
        SELECT * FROM position_changes 
        WHERE profile_id = ? AND date(timestamp) = ? 
        ORDER BY timestamp DESC
    """, (profile['id'], date)).fetchall()
    
    # Get Metrics
    metrics = get_daily_pnl_metrics(c, profile['id'], date)
        
    conn.close()
    return render_template('daily_view.html', 
                         slug=slug, 
                         date=date, 
                         changes=changes,
                         metrics=metrics)

@app.route('/api/diff/<int:change_id>')
def api_diff(change_id):
    conn = get_db()
    c = conn.cursor()
    
    change = c.execute("SELECT * FROM position_changes WHERE id = ?", (change_id,)).fetchone()
    if not change:
        conn.close()
        return jsonify({'error': 'Change not found'}), 404
        
    current_snapshot = c.execute("SELECT * FROM snapshots WHERE id = ?", (change['snapshot_id'],)).fetchone()
    current_raw = json.loads(current_snapshot['raw_data']) if current_snapshot else {}
    current_trades = normalize_trades_for_diff(current_raw.get('data', []))

    # Find PREVIOUS snapshot for this profile
    # We want the latest snapshot BEFORE this one
    prev_snapshot = c.execute("""
        SELECT * FROM snapshots 
        WHERE profile_id = ? AND id < ? 
        ORDER BY id DESC LIMIT 1
    """, (change['profile_id'], change['snapshot_id'])).fetchone()
    
    prev_raw = json.loads(prev_snapshot['raw_data']) if prev_snapshot else {}
    prev_trades = normalize_trades_for_diff(prev_raw.get('data', []))
    
    # Calculate Diff
    diff_data = calculate_diff(prev_trades, current_trades)
    
    conn.close()
    return jsonify({
        'diff_summary': change['diff_summary'],
        'positions': current_raw.get('data', []), # Send full current positions for the bottom table
        'diff': diff_data
    })

@app.route('/api/daily_log/<slug>/<date>')
def daily_log(slug, date):
    conn = get_db()
    c = conn.cursor()
    
    profile = c.execute("SELECT * FROM profiles WHERE slug = ?", (slug,)).fetchone()
    if not profile:
        conn.close()
        return jsonify({'error': 'Profile not found'}), 404
        
    # Get metrics for the day to find 'start_day_pnl'
    metrics = get_daily_pnl_metrics(c, profile['id'], date)
    start_day_pnl = metrics['start_pnl']
        
    # fetch all changes for the day in chronological order
    changes = c.execute("""
        SELECT * FROM position_changes 
        WHERE profile_id = ? AND date(timestamp) = ? 
        ORDER BY timestamp ASC
    """, (profile['id'], date)).fetchall()
    
    events = []
    
    for i, change in enumerate(changes):
        # Calculate P&L at this snapshot
        snap_total, snap_booked = calculate_snapshot_pnl(c, change['snapshot_id'])
        todays_pnl = snap_total - start_day_pnl
        
        # Calculate Detailed Diff (Restore "Change" column detail)
        curr_snap = c.execute("SELECT raw_data FROM snapshots WHERE id = ?", (change['snapshot_id'],)).fetchone()
        curr_raw = json.loads(curr_snap['raw_data']) if curr_snap else {}
        curr_trades = normalize_trades_for_diff(curr_raw.get('data', []))
        
        # Find previous snapshot (relative to this change)
        prev_snap = c.execute("""
            SELECT raw_data FROM snapshots 
            WHERE profile_id = ? AND id < ? 
            ORDER BY id DESC LIMIT 1
        """, (profile['id'], change['snapshot_id'])).fetchone()
        
        prev_raw = json.loads(prev_snap['raw_data']) if prev_snap else {}
        prev_trades = normalize_trades_for_diff(prev_raw.get('data', []))
        
        diff_data = calculate_diff(prev_trades, curr_trades)
        
        # Build Detailed List
        detailed_changes = []
        for item in diff_data['added']:
            detailed_changes.append({
                'symbol': item['trading_symbol'],
                'text': f"Qty: 0 &rarr; {item['quantity']} (+{item['quantity']})",
                'color': 'green'
            })
        for item in diff_data['removed']:
            detailed_changes.append({
                'symbol': item['trading_symbol'],
                'text': f"Qty: {item['quantity']} &rarr; 0 (-{item['quantity']})",
                'color': 'red'
            })
        for item in diff_data['modified']:
            sign = '+' if item['quantity_diff'] > 0 else ''
            color = 'green' if item['quantity_diff'] > 0 else 'red'
            detailed_changes.append({
                'symbol': item['trading_symbol'],
                'text': f"Qty: {item['old_quantity']} &rarr; {item['quantity']} ({sign}{item['quantity_diff']})",
                'color': color
            })
            
        event = {
            'time': to_datetime_filter(change['timestamp']).strftime('%H:%M:%S'),
            'type': 'Change', 
            'changes': detailed_changes, 
            'change_id': change['id'],
            'todays_pnl': todays_pnl,
            'booked_pnl': snap_booked
        }
        
        events.append(event)
    
    conn.close()
    events.reverse() # Latest first
    return jsonify({'events': events})

def normalize_trades_for_diff(positions_data):
    """
    Extracts all trades and creates a signature map for easy comparison.
    Key: symbol|product|strike|option_type
    Value: Trade object (summed quantity if multiple trades exist for same key, though rare)
    """
    trades_map = {}
    for p in positions_data:
        for t in p.get('trades', []):
            # Create a unique key for the instrument
            key = f"{t.get('trading_symbol')}|{t.get('product')}"
            
            if key not in trades_map:
                trades_map[key] = {
                    'trading_symbol': t.get('trading_symbol'),
                    'product': t.get('product'),
                    'quantity': 0,
                    'average_price': 0,
                    'last_price': t.get('last_price'), # Keep for reference
                    'pnl': t.get('unbooked_pnl') # Keep for reference
                }
            
            # Weighted average for price if needed, but usually it's unique enough. 
            # Let's just sum quantity for now.
            current_qty = trades_map[key]['quantity']
            new_qty = int(t.get('quantity', 0))
            
            # Simple avg price update (approximate if multiple trades)
            total_val = (trades_map[key]['average_price'] * current_qty) + (float(t.get('average_price', 0)) * new_qty)
            trades_map[key]['quantity'] += new_qty
            if trades_map[key]['quantity'] != 0:
                trades_map[key]['average_price'] = total_val / trades_map[key]['quantity']
            
    return trades_map

def calculate_diff(prev_map, curr_map):
    added = []
    removed = []
    modified = []
    
    all_keys = set(prev_map.keys()) | set(curr_map.keys())
    
    for key in all_keys:
        p = prev_map.get(key)
        c = curr_map.get(key)
        
        if not p:
            # Added
            c['change_type'] = 'ADDED'
            added.append(c)
        elif not c:
            # Removed
            p['change_type'] = 'REMOVED'
            removed.append(p)
        else:
            # Check for modification (quantity change)
            if p['quantity'] != c['quantity']:
                c['change_type'] = 'MODIFIED'
                c['old_quantity'] = p['quantity']
                c['quantity_diff'] = c['quantity'] - p['quantity']
                modified.append(c)
                
    return {
        'added': added,
        'removed': removed,
        'modified': modified
    }

@app.route('/restart', methods=['POST'])
def restart_scraper_endpoint():
    threading.Thread(target=restart_scraper_internal).start()
    return "Restarting scraper process... It should resume in a few seconds.", 200

@app.route('/delete_date/<date>', methods=['DELETE', 'POST'])
def delete_date(date):
    try:
        conn = get_db()
        c = conn.cursor()
        
        # 1. Delete position_changes for this date
        c.execute("DELETE FROM position_changes WHERE date(timestamp) = ?", (date,))
        changes_deleted = c.rowcount
        
        # 2. Delete snapshots for this date
        # Note: Be careful if snapshots are shared (unlikely in this design) or used by latest_snapshots
        # latest_snapshots is separate, so current state is preserved.
        c.execute("DELETE FROM snapshots WHERE date(timestamp) = ?", (date,))
        snaps_deleted = c.rowcount
        
        conn.commit()
        conn.close()
        
        print(f"Deleted data for {date}: {changes_deleted} changes, {snaps_deleted} snapshots.")
        return jsonify({'success': True, 'message': f"Deleted {changes_deleted} changes and {snaps_deleted} snapshots."})
        
    except Exception as e:
        print(f"Error deleting data for {date}: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    start_live_collector()
    app.run(debug=False, host='127.0.0.1', port=PORT)
