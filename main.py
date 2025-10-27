# =========================
# main.py — Athena + Chris 2025
# ITG Scalper Bot (Limit-only, Candle-Low Stops)
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
# Helpers
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
# Limit-only submitter
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
# Managed Exit (limit-only)
# ──────────────────────────────
def managed_exit(sym, qty_hint, target_price=None):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0: return

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
            log(f"⚠️ Aggressive limit fallback {sym} @ {fallback_price}")
            end_time = datetime.now(NY) + timedelta(minutes=5)
            while datetime.now(NY) < end_time and safe_qty(sym) > 0:
                cancel_all(sym)
                submit_limit("sell", sym, safe_qty(sym), fallback_price, extended=True)
                time.sleep(3)

        if safe_qty(sym) <= 0:
            update_pnl(sym, limit_price)
            log(f"✅ Closed {sym}")
        else:
            log(f"⚠️ Could not close {sym} fully.")

    except Exception as e:
        log(f"❌ managed_exit {sym}: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# Trade logic
# ──────────────────────────────
open_add_tracker = {}     # one add per ticker
loss_tracker = {}         # max two losses per ticker

def within_vol_window():
    now = datetime.now(NY).time()
    return datetime.strptime("09:30","%H:%M").time() <= now <= datetime.strptime("09:45","%H:%M").time()

def get_stop(sym, entry_price, signal_low):
    if within_vol_window():
        atr_buffer = entry_price * 0.03  # wider during open
        stop = min(signal_low, entry_price - atr_buffer)
    else:
        stop = signal_low
    return round_tick(stop)

def valid_candle_range(close_p, low_p):
    rng = (close_p - low_p) / close_p * 100 if close_p else 0
    log(f"🔎 Entry range (low→close): {rng:.2f}%")
    return rng <= 10

def record_loss(sym):
    loss_tracker[sym] = loss_tracker.get(sym, 0) + 1
    if loss_tracker[sym] >= 2:
        log(f"🚫 {sym} locked out after 2 losses.")

def can_trade(sym):
    return loss_tracker.get(sym, 0) < 2

def execute_buy(sym, qty, entry_price, signal_low):
    if not can_trade(sym):
        log(f"🚫 Skipping {sym}: reached loss limit.")
        return
    if safe_qty(sym) > 0:
        log(f"⏩ Already in position {sym}, skip BUY.")
        return
    if not valid_candle_range(entry_price, signal_low):
        log(f"⚠️ Skipped {sym}: >10% low→close.")
        return

    stop_price = get_stop(sym, entry_price, signal_low)
    log(f"🟢 BUY {sym} @ {entry_price} | Stop {stop_price}")
    submit_limit("buy", sym, qty, entry_price, extended=True)

def execute_add(sym, qty, entry_price):
    if not safe_qty(sym):
        log(f"⚠️ No open position for {sym}, skip ADD.")
        return
    if open_add_tracker.get(sym):
        log(f"⚠️ Add already used for {sym}.")
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
            execute_buy(sym, qty, entry, signal_low)

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
# Ping
# ──────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, service="tv→alpaca", base=ALPACA_BASE_URL)































