# main.py — v2.7 (Option-2 Exit + ATR window fix + endpoint restore)
# © Chris / Athena 2025

from flask import Flask, request, jsonify
import os, json, time, threading, traceback, math
from datetime import datetime
from zoneinfo import ZoneInfo
from alpaca_trade_api.rest import REST

app = Flask(__name__)

# ──────────────────────────────────────────────
# ENV / CLIENT
# ──────────────────────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
NY  = ZoneInfo("America/New_York")
TRADE_LOG_PATH = "/app/trade_log.json"

state = {}   # in-memory runtime info

# ──────────────────────────────────────────────
# UTIL
# ──────────────────────────────────────────────
def ts(): return datetime.now(NY).strftime("[%H:%M:%S]")
def nowNY(): return datetime.now(NY)
def log(msg): print(f"{ts()} {msg}", flush=True)

def is_opening_vol_window(dt=None):
    dt = dt or nowNY()
    start = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    end   = dt.replace(hour=9, minute=45, second=0, microsecond=0)
    return start <= dt <= end

def round_tick(p):
    p = float(p)
    step = 0.01 if p >= 1 else (0.001 if p >= 0.1 else 0.0001)
    return float(f"{math.floor(p/step)*step:.6f}")

def safe_qty(sym):
    try:
        pos = api.get_position(sym);  return float(pos.qty)
    except Exception: return 0.0

def get_position_info(sym):
    try:
        pos = api.get_position(sym);  return float(pos.qty), float(pos.avg_entry_price)
    except Exception: return 0.0, 0.0

def latest_bid_ask_trade(sym):
    bid = ask = last = None
    try:
        q = api.get_latest_quote(sym)
        bid = float(q.bidprice) if q and q.bidprice else None
        ask = float(q.askprice) if q and q.askprice else None
    except: pass
    try:
        t = api.get_latest_trade(sym)
        last = float(t.price) if t and t.price else None
    except: pass
    return bid, ask, last

def cancel_open_orders(sym):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == sym:
                api.cancel_order(o.id)
        log(f"🧹 Cancelled open orders for {sym}")
    except Exception as e:
        log(f"⚠️ cancel_open_orders({sym}) failed: {e}")

def limit_order(side, sym, qty, price):
    price = round_tick(price)
    try:
        return api.submit_order(
            symbol=sym,
            side=side,
            qty=str(qty),
            type="limit",
            limit_price=str(price),
            time_in_force="day",
            extended_hours=True
        )
    except Exception as e:
        log(f"❌ {side.upper()} limit error {sym} @{price}: {e}")
        return None

# ──────────────────────────────────────────────
# STOP LEVEL CALC (ATR×3 window vs candle-low)
# ──────────────────────────────────────────────
def compute_stop_price(candle_low, candle_close, atr_value):
    cl_low, cl_close = float(candle_low), float(candle_close)
    if is_opening_vol_window() and atr_value is not None:
        return min(cl_close, max(cl_low, cl_close - 3.0 * float(atr_value)))
    return cl_low

# ──────────────────────────────────────────────
# RANGE GUARD
# ──────────────────────────────────────────────
def passes_range_guard(candle_low, candle_close, max_pct=10.0):
    try:
        low, close = float(candle_low), float(candle_close)
        if close <= 0: return False
        return ((close - low) / close) * 100.0 <= max_pct
    except: return True

# ──────────────────────────────────────────────
# MONITOR + AGGRESSIVE EXIT
# ──────────────────────────────────────────────
def pnl_close_record(sym, exit_price, reason):
    qty, avg = get_position_info(sym)
    pnl$ = (exit_price - avg) * qty
    pnl% = ((exit_price / avg) - 1) * 100 if avg else 0
    log(f"💰 {sym} EXIT {reason} @{exit_price:.3f} | PnL ${pnl$:.2f} ({pnl%:.2f}%)")

def aggressive_limit_close(sym, start_px, reason):
    qty, _ = get_position_info(sym)
    if qty <= 0: return
    tick = 0.01
    for i in range(20):
        if safe_qty(sym) <= 0: break
        cancel_open_orders(sym)
        bid, ask, last = latest_bid_ask_trade(sym)
        ref = bid or last or ask or start_px
        px  = max(round_tick(ref - tick), tick)
        limit_order("sell", sym, qty, px)
        log(f"⏱ Aggressive EXIT {sym} try {i+1}/20 @ {px}")
        time.sleep(2)
    pnl_close_record(sym, px, reason)

def monitor_stop_until_flat(sym, stop):
    while True:
        qty, _ = get_position_info(sym)
        if qty <= 0: return
        bid, ask, last = latest_bid_ask_trade(sym)
        ref = last or bid or ask
        if ref and ref <= stop:
            log(f"🛑 Stop hit {sym} <= {stop} → close")
            aggressive_limit_close(sym, stop, "STOP")
            return
        time.sleep(1)

# ──────────────────────────────────────────────
# WEBHOOK
# ──────────────────────────────────────────────
@app.post("/tv")
def tv():
    try:
        d = request.get_json(force=True)
        if d.get("secret") != WEBHOOK_SECRET:
            return jsonify(err="bad secret"), 403

        act = d.get("action","").upper()
        sym = d.get("ticker","").upper()
        qty = float(d.get("quantity", 0))
        ep  = float(d.get("entry_price", 0) or 0)
        ex  = float(d.get("exit_price", 0)  or 0)
        low = d.get("candle_low"); close = d.get("candle_close"); atr = d.get("atr")

        cancel_open_orders(sym)

        # BUY
        if act in ("BUY","HAMMER_BUY"):
            if safe_qty(sym) > 0:
                log(f"ℹ️ {sym} BUY ignored; already in position")
                return jsonify(status="already_in_position"), 200
            if not passes_range_guard(low, close):
                log(f"🚫 {sym} range guard fail");  return jsonify(status="blocked"), 200
            o = limit_order("buy", sym, qty, ep)
            if o:
                stop_lvl = compute_stop_price(low, close, atr)
                threading.Thread(target=monitor_stop_until_flat, args=(sym, stop_lvl), daemon=True).start()
                log(f"✅ BUY {sym} @ {ep} stop {stop_lvl}")
                return jsonify(status="buy_ok"), 200
            return jsonify(err="buy_fail"), 500

        # ADD
        if act in ("ADD","HAMMER_ADD"):
            qty_now, avg = get_position_info(sym)
            if qty_now <= 0:
                return jsonify(status="no_position"), 200
            bid, ask, last = latest_bid_ask_trade(sym)
            ref = last or bid or ask or ep
            if ref <= avg:
                log(f"🚫 {sym} add blocked (not in profit)")
                return jsonify(status="add_blocked"), 200
            o = limit_order("buy", sym, qty, ep)
            if o:
                log(f"➕ ADD {sym} @ {ep}")
                return jsonify(status="add_ok"), 200

        # EXIT
        if act == "EXIT":
            qty_now, _ = get_position_info(sym)
            if qty_now <= 0:
                return jsonify(status="no_position"), 200
            if ex:
                limit_order("sell", sym, qty_now, ex)
                time.sleep(6)
                if safe_qty(sym) <= 0:
                    pnl_close_record(sym, ex, "EXIT_TARGET")
                    return jsonify(status="exit_filled"), 200
            threading.Thread(target=aggressive_limit_close, args=(sym, ex or ep, "EXIT_FALLBACK"), daemon=True).start()
            return jsonify(status="exit_aggressive"), 200

        return jsonify(status="ignored"), 200

    except Exception as e:
        log(f"❌ /tv error {e}\n{traceback.format_exc()}")
        return jsonify(err="server"), 500

# ──────────────────────────────────────────────
# HEALTH
# ──────────────────────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, service="tv→alpaca", base=ALPACA_BASE_URL)

# ──────────────────────────────────────────────
# RUN
# ──────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))





































