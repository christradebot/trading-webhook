from flask import Flask, request, jsonify
import os, json, time, threading, traceback, math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from alpaca_trade_api.rest import REST

# ──────────────────────────────────────────────
# ENV / CLIENT
# ──────────────────────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")

app = Flask(__name__)
NY = ZoneInfo("America/New_York")

# ──────────────────────────────────────────────
# STATE (per-symbol, consolidated)
# ──────────────────────────────────────────────
# state[sym] = {
#   "add_used": bool,
#   "stop": float|None,
#   "entry": float|None
# }
state = {}
state_lock = threading.Lock()
watchers = {}  # sym -> Thread

# ──────────────────────────────────────────────
# UTIL
# ──────────────────────────────────────────────
def ts(): return datetime.now(NY).strftime("[%H:%M:%S]")
def log(msg): print(f"{ts()} {msg}", flush=True)

def nowNY(): return datetime.now(NY)
def in_open_window(dt=None):
    """Checks for the initial volatile 9:30-9:45 ET window."""
    dt = dt or nowNY()
    s = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    e = dt.replace(hour=9, minute=45, second=0, microsecond=0)
    return s <= dt <= e

def round_tick(p: float) -> float:
    """Rounds price to the appropriate market tick size based on price level."""
    p = float(p)
    if p >= 1.0:
        step = 0.01
    elif p >= 0.1:
        step = 0.001
    else:
        step = 0.0001
    # Use math.floor to ensure we round down to the nearest valid tick (crucial for stops)
    return float(f"{math.floor(p/step)*step:.6f}")

def latest_bid_ask_trade(sym):
    """Fetches real-time price data with robust error handling."""
    bid = ask = last = None
    try:
        q = api.get_latest_quote(sym)
        if q:
            bid = float(q.bidprice) if q.bidprice else None
            ask = float(q.askprice) if q.askprice else None
    except Exception:
        pass
    try:
        t = api.get_latest_trade(sym)
        if t and t.price:
            last = float(t.price)
    except Exception:
        pass
    return bid, ask, last

def safe_qty(sym) -> float:
    """Returns current position quantity."""
    try:
        pos = api.get_position(sym)
        return float(pos.qty)
    except Exception:
        return 0.0

def pos_avg(sym) -> float:
    """Returns average entry price."""
    try:
        pos = api.get_position(sym)
        return float(pos.avg_entry_price)
    except Exception:
        return 0.0

def cancel_open_orders(sym):
    """Cancels all currently open orders for a specific symbol."""
    try:
        # Fetch only open orders for the symbol to reduce API load
        for o in api.list_orders(status="open"):
            if o.symbol == sym:
                api.cancel_order(o.id)
        # log(f"🧹 Cancelled open orders for {sym}") # Too noisy for every webhook
    except Exception as e:
        log(f"⚠️ cancel_open_orders({sym}) failed: {e}")

def limit_order(side, sym, qty, price):
    """Submits a limit order."""
    price = round_tick(price)
    try:
        o = api.submit_order(
            symbol=sym,
            side=side,
            qty=str(int(qty) if float(qty).is_integer() else qty),
            type="limit",
            limit_price=str(price),
            time_in_force="day",
            extended_hours=True
        )
        log(f"📥 {side.upper()} LIMIT {sym} x{qty} @ {price}")
        return o
    except Exception as e:
        log(f"❌ {side.upper()} limit error {sym} @{price}: {e}")
        return None

def range_guard_ok(signal_low, signal_close, max_pct=11.0):
    """Ensures the signal candle range (Low to Close) is not excessively large."""
    try:
        lo = float(signal_low)
        cl = float(signal_close)
        if cl <= 0:
            return True
        pct = ((cl - lo) / cl) * 100.0
        log(f"🔎 Low→Close range: {pct:.2f}% (≤ {max_pct}% required)")
        return pct <= max_pct
    except Exception:
        log("ℹ️ Range guard skipped (no valid signal_low/signal_close).")
        return True

def compute_stop(entry, signal_low):
    """Calculates the stop-loss level based on entry price, signal low, and time window."""
    entry = float(entry)
    lo = None if signal_low is None else float(signal_low)

    if in_open_window():
        # During 9:30-9:45 ET, enforce a tight stop using min(Signal_Low, 3% hard stop)
        if lo is not None and lo > 0:
            return round_tick(min(lo, entry * 0.97))
        return round_tick(entry * 0.97) # Fallback to 3% hard stop
    else:
        # Outside open window, only use the technical signal low if provided
        if lo is not None and lo > 0:
            return round_tick(lo)
        # Cannot determine a stop if no candle low is provided
        return None

def record_realized(sym, exit_price, reason):
    """Logs the exit transaction and indicative PnL."""
    # Use last known avg (before flat)
    avg = pos_avg(sym)
    pnl_p = 0.0
    try:
        if avg and avg > 0:
            pnl_p = (float(exit_price) / avg - 1.0) * 100.0
    except Exception:
        pass

    log(f"💰 EXIT {sym} @{round_tick(float(exit_price))} | reason={reason} | PnL%≈{pnl_p:.2f}")

# ──────────────────────────────────────────────
# AGGRESSIVE LIMIT EXIT LOOP
# ──────────────────────────────────────────────
def aggressive_close(sym, suggested_price, reason, max_iters=20, sleep_s=2.0):
    """
    Keep posting descending limit sells near bid until flat. Runs on a separate thread.
    """
    tries = 0
    # Determine tick size dynamically
    _, _, last = latest_bid_ask_trade(sym)
    if last is not None:
        tick = 0.01 if last >= 1 else (0.001 if last >= 0.1 else 0.0001)
    else:
        tick = 0.01 # Default

    px = round_tick(float(suggested_price))
    while tries < max_iters:
        current_qty = safe_qty(sym)
        if current_qty <= 0:
            record_realized(sym, px, reason)
            with state_lock:
                st = state.get(sym, {})
                st["stop"] = None # Clear stop when position is flat
                state[sym] = st
            return

        cancel_open_orders(sym)
        bid, ask, last = latest_bid_ask_trade(sym)
        ref = bid or last or px
        if ref is None or ref <= 0:
            ref = px

        # Set limit slightly below the current best bid (aggressive for guaranteed fill)
        px = max(round_tick(ref - tick), tick)
        limit_order("sell", sym, current_qty, px)
        tries += 1
        log(f"⏱ {sym} aggressive exit {tries}/{max_iters} @ {px} ({reason})")
        time.sleep(sleep_s)

    # If still holding after max attempts, try one final aggressive push
    if safe_qty(sym) > 0:
        bid, ask, last = latest_bid_ask_trade(sym)
        ref = bid or last or px
        px = max(round_tick(ref - tick), tick)
        cancel_open_orders(sym)
        limit_order("sell", sym, safe_qty(sym), px)
        log(f"⚠️ {sym} final exit push @ {px} ({reason})")

    time.sleep(2) # Give time for final order to process
    current_qty = safe_qty(sym)
    if current_qty > 0:
        log(f"❗ Failed to fully flat {sym}. Remaining Qty: {current_qty}")
    record_realized(sym, px, f"{reason}_FINAL")

# ──────────────────────────────────────────────
# STOP WATCHER (polling Alpaca; no pre-placed stop orders)
# ──────────────────────────────────────────────
def ensure_watcher(sym):
    """Starts the stop-loss monitoring thread if not already running."""
    with state_lock:
        if sym in watchers and watchers[sym].is_alive():
            return
        t = threading.Thread(target=watch_loop, args=(sym,), daemon=True)
        watchers[sym] = t
        t.start()
        log(f"🚀 Started new stop watcher for {sym}")

def watch_loop(sym):
    log(f"👀 Stop watcher started for {sym}")
    try:
        while True:
            time.sleep(1.0)
            with state_lock:
                st = state.get(sym, {})
                stop = st.get("stop")
           
            current_qty = safe_qty(sym)
            if current_qty <= 0:
                with state_lock:
                    st["stop"] = None
                    state[sym] = st
                break # Position is flat, stop watching
           
            if stop is None:
                continue # No stop level set, continue watching for a stop to be set

            bid, ask, last = latest_bid_ask_trade(sym)
            ref = last or bid or ask
            if ref is None:
                continue
           
            # Check for stop breach
            if ref <= float(stop):
                log(f"🛑 Stop breach {sym}: last={ref:.6f} ≤ stop={float(stop):.6f} → aggressive close")
                # Aggressive close runs on its own thread, no need to thread this one
                cancel_open_orders(sym)
                aggressive_close(sym, float(stop), reason="STOP")
                break
    except Exception as e:
        log(f"❌ watcher({sym}) error: {e}\n{traceback.format_exc()}")
    finally:
        # CRITICAL: Clean up the watchers dictionary to prevent memory leak
        with state_lock:
             watchers.pop(sym, None)
        log(f"🧹 Stop watcher ended for {sym}")

# ──────────────────────────────────────────────
# CORE HANDLERS
# ──────────────────────────────────────────────
def handle_buy(sym, qty, entry_price, signal_low=None, signal_close=None, source=""):
    # 1. Validation (Range Guard)
    if not range_guard_ok(signal_low, signal_close, 11.0):
        log(f"🚫 {sym} BUY blocked by 11% range guard.")
        return jsonify(status="blocked_range_guard"), 200

    # 2. Validation (Check if already in position)
    if safe_qty(sym) > 0:
        log(f"ℹ️ {sym} BUY ignored; already in a position.")
        return jsonify(status="already_in_position"), 200

    # 3. Execution
    ep = round_tick(float(entry_price))
    if not limit_order("buy", sym, qty, ep):
        return jsonify(err="buy_order_failed"), 500

    # 4. State Update (after order submission)
    stop_lvl = compute_stop(entry=ep, signal_low=signal_low)
    with state_lock:
        st = state.get(sym, {"add_used": False, "stop": None, "entry": None})
        st["entry"] = ep
        st["stop"] = stop_lvl
        state[sym] = st

    # 5. Start Watcher
    ensure_watcher(sym)
    log(f"✅ {sym} BUY placed @ {ep} | stop={round_tick(st['stop']) if st['stop'] else 'None'} | src={source}")
    return jsonify(status="buy_ok"), 200

def handle_add(sym, qty, entry_price, source=""):
    # 1. Validation (Position Check)
    if safe_qty(sym) <= 0:
        log(f"ℹ️ {sym} ADD ignored; no open position.")
        return jsonify(status="no_position"), 200
   
    # 2. Validation (Add Used Check)
    with state_lock:
        st = state.get(sym, {"add_used": False})
        already = st.get("add_used", False)
    if already:
        log(f"ℹ️ {sym} ADD ignored; already used once.")
        return jsonify(status="add_already_used"), 200

    # 3. Validation (Profit Check)
    cur = None
    bid, ask, last = latest_bid_ask_trade(sym)
    cur = last or bid or ask
    avg = pos_avg(sym)
    # Block ADD if price is not strictly above average entry price
    if cur is None or avg <= 0 or cur <= avg:
        log(f"🚫 {sym} ADD blocked; not in profit. cur={cur}, avg={avg}")
        return jsonify(status="add_blocked_not_profitable"), 200

    # 4. Execution
    ap = round_tick(float(entry_price))
    if not limit_order("buy", sym, qty, ap):
        return jsonify(err="add_order_failed"), 500

    # 5. State Update
    with state_lock:
        st["add_used"] = True
        state[sym] = st

    log(f"➕ {sym} ADD placed @ {ap} | src={source}")
    return jsonify(status="add_ok"), 200

def handle_exit(sym, target_price=None):
    if safe_qty(sym) <= 0:
        log(f"ℹ️ {sym} EXIT ignored; flat.")
        return jsonify(status="no_position"), 200
   
    # Clear stop watcher immediately
    with state_lock:
        st = state.get(sym, {})
        st["stop"] = None
        state[sym] = st

    if target_price is not None and float(target_price) > 0:
        tgt = round_tick(float(target_price))
        log(f"🔔 {sym} EXIT try alert target @{tgt}")
        cancel_open_orders(sym)
        limit_order("sell", sym, safe_qty(sym), tgt)
        time.sleep(6) # Wait for fill attempt

        if safe_qty(sym) <= 0:
            # Filled at target
            record_realized(sym, tgt, reason="EXIT_ALERT_TARGET")
            return jsonify(status="exit_filled_at_target"), 200

        # Target failed → fallback to aggressive close on separate thread
        threading.Thread(target=aggressive_close, args=(sym, tgt, "EXIT_ALERT_FALLBACK"), daemon=True).start()
        return jsonify(status="exit_aggressive_started"), 200

    # No target provided → start aggressive at bid/last immediately
    bid, ask, last = latest_bid_ask_trade(sym)
    start_px = bid or last or ask
    if not start_px:
        return jsonify(err="no_price_for_exit"), 500
    threading.Thread(target=aggressive_close, args=(sym, float(start_px), "EXIT_NO_TARGET"), daemon=True).start()
    return jsonify(status="exit_aggressive_started"), 200

# ──────────────────────────────────────────────
# DAILY AUTO-FLAT @ 19:59 ET
# ──────────────────────────────────────────────
def eod_liquidator():
    log("⏰ EOD liquidator started (auto-flat before 20:00 ET)")
    while True:
        try:
            now = nowNY()
            # Target is 19:59:00 on the current or next day
            cutoff = now.replace(hour=19, minute=59, second=0, microsecond=0)

            # If current time is after the target, set cutoff for the next day
            if now >= cutoff:
                # Use timedelta(days=1) for robust date arithmetic
                cutoff = (now + timedelta(days=1)).replace(hour=19, minute=59, second=0, microsecond=0)

            sleep_s = (cutoff - now).total_seconds()
            # Sleep at least 1 second, but no more than 60 seconds for frequent checking
            time.sleep(min(max(sleep_s, 1), 60))

            # Re-check time
            now = nowNY()
            if now.hour == 19 and now.minute == 59:
                log("🌙 Daily cleanup: closing all positions before overnight.")
               
                # 1. Cancel all open orders
                try:
                    for o in api.list_orders(status="open"):
                        api.cancel_order(o.id)
                    log("🧹 All open orders cancelled for EOD.")
                except Exception as e:
                    log(f"EOD cancel error: {e}")

                # 2. Liquidate all open longs
                try:
                    positions = api.list_positions()
                    for p in positions:
                        sym = p.symbol
                        qty = float(p.qty)
                        if qty > 0:
                            # Use aggressive_close for robust liquidation
                            log(f"🔥 EOD Liquidating {sym} x{qty}")
                            # Use current entry price as a conservative suggested price to start the aggressive loop
                            aggressive_close(sym, pos_avg(sym), reason="EOD_FLAT", max_iters=5, sleep_s=1.0)
                except Exception as e:
                    log(f"EOD liquidate error: {e}")

                # After liquidation, force a long sleep to avoid re-triggering immediately
                time.sleep(65) # Sleep past 20:00:00 to wait for the next day's 19:59
        except Exception as e:
            log(f"❌ EOD liquidator loop error: {e}\n{traceback.format_exc()}")
            time.sleep(5)

# ──────────────────────────────────────────────
# WEBHOOK
# ──────────────────────────────────────────────
@app.post("/tv")
def tv():
    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify(err="bad_json"), 400

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(err="invalid_secret"), 403

    sym    = str(data.get("ticker", "")).upper()
    action = str(data.get("action", "")).upper()
    if not sym or not action:
        return jsonify(err="missing_symbol_or_action"), 400

    qty          = float(data.get("quantity", 100))
    entry_price  = data.get("entry_price", None)
    exit_price   = data.get("exit_price", None)
    signal_low   = data.get("signal_low", None)
    signal_close = data.get("signal_close", None)
    source       = str(data.get("source", ""))

    # Normalize empty strings and ensure fields are correct numeric type
    def nfloat(x):
        try:
            return None if x in ("", None) else float(x)
        except Exception:
            return None
    entry_price  = nfloat(entry_price)
    exit_price   = nfloat(exit_price)
    signal_low   = nfloat(signal_low)
    signal_close = nfloat(signal_close)

    # Cancel strays on every intent (prevents old orders from interfering)
    cancel_open_orders(sym)

    if action in ("BUY", "HAMMER_BUY", "SCALPER_BUY"):
        if entry_price is None:
            return jsonify(err="missing_entry_price"), 400
        return handle_buy(sym, qty, entry_price, signal_low, signal_close, source)

    if action in ("ADD", "HAMMER_ADD", "SCALPER_ADD"):
        if entry_price is None:
            return jsonify(err="missing_entry_price"), 400
        return handle_add(sym, qty, entry_price, source)

    if action == "EXIT":
        return handle_exit(sym, exit_price)

    return jsonify(status="ignored_unknown_action"), 200

# ──────────────────────────────────────────────
# HEALTH
# ──────────────────────────────────────────────
@app.get("/ping")
def ping():
    try:
        return jsonify(ok=True, service="tv→alpaca", base=ALPACA_BASE_URL, time=str(nowNY()))
    except Exception as e:
        return jsonify(ok=False, error=str(e)), 500

# ──────────────────────────────────────────────
# RUN (container)
# ──────────────────────────────────────────────
if __name__ == "__main__":
    # Start the critical End-of-Day liquidator thread
    threading.Thread(target=eod_liquidator, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

















































