# =========================
# main.py — Athena + Chris 2025
# ITG Scalper + Hammer Logic (v3.1 — source-aware logs)
# =========================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
from datetime import datetime, timedelta
import os, time, pytz, threading, traceback

# ──────────────────────────────
# ENV + CLIENT
# ──────────────────────────────
ALPACA_KEY_ID     = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
app = Flask(__name__)
NY = pytz.timezone("America/New_York")

# ──────────────────────────────
# STATE
# ──────────────────────────────
stops, watchers, open_add_tracker, loss_tracker = {}, {}, {}, {}
lock = threading.Lock()
ENTRY_BUFFER_PCT = 0.002  # 0.2 % buffer above signal high

# ──────────────────────────────
# HELPERS
# ──────────────────────────────
def log(msg): print(f"{datetime.now().strftime('%H:%M:%S')} | {msg}", flush=True)

def round_tick(px): return round(px, 4) if px < 1 else round(px, 2)

def latest_bid_ask(sym):
    try:
        q = api.get_latest_quote(sym)
        return float(q.bidprice or 0), float(q.askprice or 0)
    except Exception: return 0, 0

def last_trade_price(sym):
    bid, ask = latest_bid_ask(sym)
    if bid > 0: return bid
    try:
        t = api.get_latest_trade(sym)
        return float(getattr(t, "price", 0.0) or 0.0)
    except Exception: return 0

def safe_qty(sym):
    try: return float(api.get_position(sym).qty)
    except Exception: return 0

def avg_entry_price(sym):
    try: return float(api.get_position(sym).avg_entry_price)
    except Exception: return 0

def cancel_all(sym):
    try:
        for o in api.list_orders(status="open", symbols=[sym]): api.cancel_order(o.id)
    except Exception: pass

# ──────────────────────────────
# RANGE / STOP / LOSS
# ──────────────────────────────
def within_vol_window():
    now = datetime.now(NY).time()
    return datetime.strptime("09:30","%H:%M").time() <= now <= datetime.strptime("09:45","%H:%M").time()

def get_stop(entry_price, signal_low):
    guard = entry_price * 0.03 if within_vol_window() else 0
    return round_tick(min(signal_low, entry_price - guard))

def valid_candle_range(close_p, low_p):
    rng = (close_p - low_p) / close_p * 100 if close_p else 0
    log(f"🔎 Range low→close {rng:.2f}%")
    return rng <= 11

def record_loss(sym):
    with lock:
        loss_tracker[sym] = loss_tracker.get(sym, 0) + 1
        if loss_tracker[sym] >= 2: log(f"🚫 {sym} locked after 2 losses")

def can_trade(sym): return loss_tracker.get(sym, 0) < 2

# ──────────────────────────────
# ORDERS / PnL
# ──────────────────────────────
def submit_limit(side, sym, qty, px):
    try:
        api.submit_order(symbol=sym, qty=int(qty), side=side, type="limit",
                         limit_price=round_tick(px), time_in_force="day",
                         extended_hours=True)
        log(f"📥 {side.upper()} LIMIT {sym} @ {round_tick(px)} x{int(qty)}")
    except Exception as e: log(f"⚠️ submit_limit {sym}: {e}")

def update_pnl(sym, exit_price, source):
    try:
        avg, qty = avg_entry_price(sym), safe_qty(sym)
        pnl_d = (exit_price - avg) * qty
        pnl_p = ((exit_price / avg) - 1) * 100 if avg > 0 else 0
        log(f"💰 {sym} EXIT ({source}) @ {exit_price:.4f} | PnL ${pnl_d:.2f} ({pnl_p:.2f}%)")
    except Exception:
        log(f"💰 {sym} EXIT ({source}) @ {exit_price}")

# ──────────────────────────────
# EXIT MANAGER
# ──────────────────────────────
def managed_exit(sym, qty_hint, target_price=None, mark_stop_loss=False, source="GENERIC"):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0: return
        px = round_tick(target_price or 0)
        if px <= 0:
            bid, ask = latest_bid_ask(sym)
            px = round_tick(bid or ask)
        cancel_all(sym); submit_limit("sell", sym, qty, px)
        time.sleep(6)
        step = 0.0005 if px < 1 else 0.02
        while safe_qty(sym) > 0:
            px = round_tick(px - step)
            cancel_all(sym)
            submit_limit("sell", sym, safe_qty(sym), px)
            time.sleep(2)
        if safe_qty(sym) <= 0:
            update_pnl(sym, px, source)
            with lock:
                stops.pop(sym, None); open_add_tracker.pop(sym, None)
            if mark_stop_loss: record_loss(sym)
    except Exception as e: log(f"❌ managed_exit {sym}: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# STOP WATCHER
# ──────────────────────────────
def stop_watcher(sym, source):
    log(f"👀 Stop watcher for {sym} ({source})")
    while True:
        time.sleep(3)
        info = stops.get(sym)
        if not info or safe_qty(sym) <= 0: break
        stop_price = info["stop"]
        last = last_trade_price(sym)
        if last and last <= stop_price:
            log(f"🛑 Stop hit {sym} ({source}) last {last} ≤ {stop_price}")
            managed_exit(sym, safe_qty(sym), stop_price, True, source); break

def ensure_watcher(sym, source):
    with lock:
        if sym in watchers and watchers[sym].is_alive(): return
        t = threading.Thread(target=stop_watcher, args=(sym, source), daemon=True)
        watchers[sym] = t; t.start()

# ──────────────────────────────
# ENTRY HELPERS
# ──────────────────────────────
def entry_trigger_passed(sym, high_price):
    try: return last_trade_price(sym) >= high_price * (1 + ENTRY_BUFFER_PCT)
    except Exception: return False

# ──────────────────────────────
# ACTIONS
# ──────────────────────────────
def execute_buy(sym, qty, high, low, close, source):
    if not can_trade(sym) or safe_qty(sym) > 0 or not valid_candle_range(close, low): return
    if not entry_trigger_passed(sym, high):
        log(f"⚠️ BUY {sym} ({source}) skipped; high not broken"); return
    entry = round_tick(high * (1 + ENTRY_BUFFER_PCT))
    stop  = get_stop(entry, low)
    log(f"🟢 BUY {sym} ({source}) @ {entry} | Stop {stop}")
    submit_limit("buy", sym, qty, entry)
    with lock: stops[sym] = {"stop": stop, "entry": entry}
    ensure_watcher(sym, source)

def execute_add(sym, qty, high, low, close, source):
    if safe_qty(sym) <= 0 or open_add_tracker.get(sym): return
    if not valid_candle_range(close, low) or not entry_trigger_passed(sym, high): return
    entry = round_tick(high * (1 + ENTRY_BUFFER_PCT))
    log(f"🔵 ADD {sym} ({source}) @ {entry}")
    submit_limit("buy", sym, qty, entry)
    open_add_tracker[sym] = True

def handle_exit(sym, qty_hint, exit_price, source):
    log(f"🔴 EXIT {sym} ({source}) triggered")
    managed_exit(sym, qty_hint, exit_price, False, source)

# ──────────────────────────────
# ALERT HANDLER
# ──────────────────────────────
def handle_alert(data):
    try:
        sym=(data.get("ticker") or "").upper()
        act=(data.get("action") or "").upper()
        src=data.get("source","GENERIC").upper()
        qty=float(data.get("quantity",100))
        high=float(data.get("signal_high",0))
        low=float(data.get("signal_low",0))
        close=float(data.get("signal_close",0))
        exitp=float(data.get("exit_price",0))
        log(f"🚀 {act} signal for {sym} ({src})")
        if act=="BUY": execute_buy(sym,qty,high,low,close,src)
        elif act=="ADD": execute_add(sym,qty,high,low,close,src)
        elif act=="EXIT": handle_exit(sym,qty,exitp,src)
        else: log(f"⚠️ Unknown action {act}")
    except Exception as e: log(f"❌ handle_alert {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# WEBHOOK
# ──────────────────────────────
@app.post("/tv")
def tv():
    d=request.get_json(silent=True) or {}
    if d.get("secret")!=WEBHOOK_SECRET: return jsonify(error="Invalid secret"),403
    threading.Thread(target=handle_alert,args=(d,),daemon=True).start()
    return jsonify(ok=True)

@app.get("/ping")
def ping(): return jsonify(ok=True,service="tv→alpaca",base=ALPACA_BASE_URL)

# ──────────────────────────────
# RUN
# ──────────────────────────────
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8080)))












































