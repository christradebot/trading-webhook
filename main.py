# =========================
# main.py — Athena + Chris 2025
# ITG Scalper Bot (Limit-only)
# =========================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
from datetime import datetime, timedelta
import os, time, json, traceback, pytz

# ──────────────────────────────
# Environment + API setup
# ──────────────────────────────
ALPACA_KEY_ID     = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
app = Flask(__name__)
NY = pytz.timezone("America/New_York")

# ──────────────────────────────
# Utility helpers
# ──────────────────────────────
def log(msg): print(f"{datetime.now().strftime('%H:%M:%S')} | {msg}")

def round_tick(px): return round(px, 2) if px else 0

def latest_bid_ask(sym):
    q = api.get_latest_quote(sym)
    return q.bidprice or 0, q.askprice or 0

def safe_qty(sym):
    try:
        pos = api.get_position(sym)
        return float(pos.qty)
    except Exception:
        return 0

def cancel_all(sym):
    for o in api.list_orders(status="open", symbols=[sym]):
        api.cancel_order(o.id)

def update_pnl(sym, price):
    log(f"💰 Recorded exit for {sym} @ {price}")

# ──────────────────────────────
# Limit-only submitters
# ──────────────────────────────
def submit_limit(side, sym, qty, px, extended=True):
    try:
        api.submit_order(
            symbol=sym,
            qty=int(qty),
            side=side,
            type="limit",
            time_in_force="day",
            limit_price=px,
            extended_hours=extended
        )
        log(f"📥 {side.upper()} LIMIT {sym} @ {px} x{qty}")
    except Exception as e:
        log(f"⚠️ submit_limit {sym}: {e}")

# ──────────────────────────────
# Managed Exit (limit only)
# ──────────────────────────────
def managed_exit(sym, qty_hint, target_price=None):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0:
            return

        limit_price = round_tick(target_price) if target_price else 0
        if limit_price <= 0:
            bid, ask = latest_bid_ask(sym)
            limit_price = round_tick(bid or ask or 0)
        if limit_price <= 0:
            log(f"⚠️ No valid exit price for {sym}, skipping.")
            return

        log(f"🟣 Exit target for {sym} @ {limit_price}")
        cancel_all(sym)
        submit_limit("sell", sym, qty, limit_price, extended=True)
        time.sleep(10)

        if safe_qty(sym) > 0:
            fallback_price = round_tick(limit_price * 0.9995)
            log(f"⚠️ Aggressive limit fallback for {sym} @ {fallback_price}")
            end_time = datetime.now(NY) + timedelta(minutes=5)
            while datetime.now(NY) < end_time and safe_qty(sym) > 0:
                cancel_all(sym)
                submit_limit("sell", sym, safe_qty(sym), fallback_price, extended=True)
                time.sleep(3)

        if safe_qty(sym) <= 0:
            update_pnl(sym, limit_price)
            log(f"✅ Position fully closed for {sym}")
        else:
            log(f"⚠️ Could not close {sym} completely.")

    except Exception as e:
        log(f"❌ managed_exit {sym}: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# Trade management logic
# ──────────────────────────────
open_add_tracker = {}  # {symbol: bool}

def within_vol_window():
    now = datetime.now(NY).time()
    return now >= datetime.strptime("09:30","%H:%M").time() and now <= datetime.strptime("09:45","%H:%M").time()

def get_stop(sym, entry_price, signal_low):
    if within_vol_window():
        atr_buffer = entry_price * 0.03  # wider 3% stop during 9:30–9:45
        stop = min(signal_low, entry_price - atr_buffer)
    else:
        stop = signal_low
    return round_tick(stop)

def valid_candle_range(open_p, close_p, high_p, low_p):
    rng = (high_p - low_p) / close_p * 100 if close_p else 0
    return rng <= 10  # must be under 10%

def execute_buy(sym, qty, entry_price, signal_low):
    if safe_qty(sym) > 0:
        log(f"⏩ Already in position {sym}, skipping BUY.")
        return
    stop_price = get_stop(sym, entry_price, signal_low)
    log(f"🟢 BUY {sym} @ {entry_price} | Stop {stop_price}")
    submit_limit("buy", sym, qty, entry_price, extended=True)

def execute_add(sym, qty, entry_price):
    if not safe_qty(sym):
        log(f"⚠️ No open position for {sym}, skipping ADD.")
        return
    if open_add_tracker.get(sym):
        log(f"⚠️ Add already used for {sym}, skipping.")
        return
    log(f"➕ ADD {sym} @ {entry_price}")
    submit_limit("buy", sym, qty, entry_price, extended=True)
    open_add_tracker[sym] = True

# ──────────────────────────────
# Webhook endpoint
# ──────────────────────────────
@app.post("/tv")
def tv():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403

    try:
        sym = data.get("ticker")
        action = data.get("action")
        qty = float(data.get("quantity", 100))
        entry = float(data.get("entry_price", 0))
        exitp = float(data.get("exit_price", 0))
        signal_low = float(data.get("signal_low", 0))

        log(f"🚀 {action} signal for {sym}")

        if action == "BUY":
            if valid_candle_range(entry, entry, entry, signal_low):
                execute_buy(sym, qty, entry, signal_low)
            else:
                log(f"⚠️ Skipped {sym}: candle >10% range.")

        elif action == "EXIT":
            managed_exit(sym, qty, exitp)

        elif action == "ADD":
            execute_add(sym, qty, entry)

        else:
            log(f"⚠️ Unknown action: {action}")

    except Exception as e:
        log(f"❌ Webhook error: {e}\n{traceback.format_exc()}")

    return jsonify(ok=True)

# ──────────────────────────────
# Heartbeat
# ──────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, service="tv→alpaca", base=ALPACA_BASE_URL)






























