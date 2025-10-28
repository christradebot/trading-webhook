# main.py — v2.6 (Option-2 Exit: alert price first, then aggressive limit loop)
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

# In-memory state
state = {}

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

def round_tick(p: float) -> float:
    p = float(p)
    if p >= 1.0:
        step = 0.01
    elif p >= 0.1:
        step = 0.001
    else:
        step = 0.0001
    return float(f"{math.floor(p/step)*step:.6f}")

def safe_qty(sym):
    try:
        pos = api.get_position(sym)
        return float(pos.qty)
    except Exception:
        return 0.0

def get_position_info(sym):
    try:
        pos = api.get_position(sym)
        return float(pos.qty), float(pos.avg_entry_price)
    except Exception:
        return 0.0, 0.0

def latest_bid_ask_trade(sym):
    bid = ask = last = None
    try:
        q = api.get_latest_quote(sym)
        bid = float(q.bidprice) if q and q.bidprice else None
        ask = float(q.askprice) if q and q.askprice else None
    except Exception:
        pass
    try:
        t = api.get_latest_trade(sym)
        last = float(t.price) if t and t.price else None
    except Exception:
        pass
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
    cl_low  = float(candle_low)
    cl_close= float(candle_close)
    if is_opening_vol_window():
        if atr_value is not None:
            # ATR×3 stop only between 9:30–9:45 ET
            return min(cl_close, max(cl_low, cl_close - 3.0 * float(atr_value)))
        return cl_low
    return cl_low

# ──────────────────────────────────────────────
# RANGE GUARD (low→close ≤ 10 %)
# ──────────────────────────────────────────────
def passes_range_guard(candle_low, candle_close, max_pct=10.0):
    try:
        low, close = float(candle_low), float(candle_close)
        if close <= 0: return False
        rng_pct = ((close - low) / close) * 100.0
        return rng_pct <= max_pct
    except Exception:
        return True

# (Other functions remain unchanged: monitor_stop_until_flat,
# aggressive_limit_close, pnl logging, and /tv endpoint logic)

# ──────────────────────────────────────────────
# PATCHED BUY SECTION (syntax fix)
# ──────────────────────────────────────────────
# Inside your /tv route:
# Replace the BUY block’s first few lines with:
"""
if safe_qty(sym) > 0:
    log(f"ℹ️ {sym} BUY ignored; already in a position")
    return jsonify(status="already_in_position"), 200
"""
# (Everything else in the file stays identical.)
# ──────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))




































