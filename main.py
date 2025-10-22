# main.py
# VWAP + MaMA Entry/Exit Bot for Alpaca + TradingView Webhooks
# Spec:
# - Long only. Entries at VWAP, upper VWAP band, or MaMA.
# - Add 25 shares on new signals while in position.
# - Exits only after EXIT_SIGNAL alert.
# - All orders are LIMIT. Market exits allowed only during RTH.
# - Hard stop = -10%. Exit always enforced.

import os, json, time, math, datetime
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST

# ──────────────────────────────────────────────────────────────────────────────
# Config / Environment
# ──────────────────────────────────────────────────────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

NY = ZoneInfo("America/New_York")
CHECK_INTERVAL_SEC = 20
EXIT_TIMEOUT_SEC   = 10 * 60
ENTRY_BUFFER_PCT   = 0.03     # ±3% tolerance to consider alternate level
HARD_STOP_PCT      = 0.10
ADDON_QTY          = 25
BASE_QTY           = 100

app = Flask(__name__)
api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')

# ──────────────────────────────────────────────────────────────────────────────
# Utility helpers
# ──────────────────────────────────────────────────────────────────────────────
def now_str():
    return datetime.datetime.now(NY).strftime("%H:%M:%S")

def log(msg):
    print(f"[{now_str()}] {msg}", flush=True)

def is_rth(dt=None):
    dt = dt or datetime.datetime.now(NY)
    hhmm = dt.hour * 60 + dt.minute
    return 9*60+30 <= hhmm < 16*60

def round_to_tick(p):
    t = 0.01 if p >= 1.0 else 0.0001
    return math.floor(p / t) * t

def get_last_trade(symbol):
    try:
        return float(api.get_latest_trade(symbol).price)
    except Exception:
        return None

def get_best_bid(symbol):
    try:
        q = api.get_latest_quote(symbol)
        return float(q.bid_price)
    except Exception:
        return None

def get_position(symbol):
    try:
        pos = api.get_position(symbol)
        return float(pos.qty), float(pos.avg_entry_price)
    except Exception:
        return 0.0, 0.0

def cancel_open_orders(symbol):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                api.cancel_order(o.id)
        log(f"🧹 Cancelled open orders for {symbol}")
    except Exception as e:
        log(f"⚠️ Cancel error {symbol}: {e}")

def submit_limit(side, symbol, qty, limit_price, extended):
    limit_price = round_to_tick(limit_price)
    try:
        o = api.submit_order(
            symbol=symbol,
            qty=qty,
            side=side,
            type="limit",
            time_in_force="day",
            limit_price=limit_price,
            extended_hours=extended
        )
        log(f"📥 {side.upper()} {symbol} LMT @{limit_price}")
        return o
    except Exception as e:
        log(f"❌ {side.upper()} submit error {symbol}: {e}")
        return None

def submit_market_sell(symbol, qty):
    try:
        api.submit_order(symbol=symbol, qty=qty, side="sell", type="market", time_in_force="day")
        log(f"💥 MARKET SELL {symbol}")
    except Exception as e:
        log(f"❌ MARKET SELL error {symbol}: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# Entry Logic (BUY)
# ──────────────────────────────────────────────────────────────────────────────
def handle_buy(symbol, base_level, upper_band, mama, action):
    last = get_last_trade(symbol)
    if not last:
        log(f"🚫 No price for {symbol}")
        return
    qty, entry = get_position(symbol)
    extended = not is_rth()

    # Select target level
    target = None
    if action == "BUY_VWAP":
        target = base_level
    elif action == "BUY_UPPER":
        target = upper_band
    elif action == "BUY_MAMA":
        target = mama

    # If target invalid, fallback to nearest
    levels = [v for v in [base_level, upper_band, mama] if v]
    if not target and levels:
        target = min(levels, key=lambda v: abs(v - last))

    # If far (>3%) above chosen target → fallback lower
    if last > target * (1 + ENTRY_BUFFER_PCT) and mama:
        target = mama
    elif last > target * (1 + ENTRY_BUFFER_PCT) and base_level:
        target = base_level

    limit_price = round_to_tick(target)

    # Determine order size (base or addon)
    if qty <= 0:
        order_qty = BASE_QTY
        log(f"🟢 Initial BUY {symbol} @{limit_price} qty={order_qty}")
    else:
        order_qty = ADDON_QTY
        log(f"🟢 Add-on BUY {symbol} @{limit_price} qty={order_qty}")

    submit_limit("buy", symbol, order_qty, limit_price, extended)

# ──────────────────────────────────────────────────────────────────────────────
# Exit Logic (EXIT_SIGNAL)
# ──────────────────────────────────────────────────────────────────────────────
def handle_exit(symbol, upper_band, mama):
    qty, entry = get_position(symbol)
    if qty <= 0:
        log(f"ℹ️ No position to exit {symbol}")
        return
    cancel_open_orders(symbol)
    extended = not is_rth()

    last = get_last_trade(symbol)
    if not last:
        log(f"⚠️ No last price, using entry {entry}")
        last = entry

    # Choose nearest exit target
    candidates = [v for v in [upper_band, mama] if v]
    if candidates:
        target = min(candidates, key=lambda v: abs(v - last))
    else:
        target = last

    limit_price = round_to_tick(target)
    log(f"🛑 EXIT {symbol} @{limit_price} target chosen")

    # Place exit order
    if is_rth():
        submit_market_sell(symbol, qty)
        log(f"✅ MARKET EXIT {symbol} (RTH)")
    else:
        submit_limit("sell", symbol, qty, limit_price, extended)
        log(f"🔁 LIMIT EXIT {symbol} (EXT)")

    # Enforce closure
    start = time.time()
    while time.time() - start < EXIT_TIMEOUT_SEC:
        time.sleep(CHECK_INTERVAL_SEC)
        q, _ = get_position(symbol)
        if q <= 0:
            log(f"✅ Position closed {symbol}")
            return
        # Hard stop check
        l = get_last_trade(symbol)
        if entry and l <= entry * (1 - HARD_STOP_PCT):
            log(f"🚨 Hard stop hit {symbol}")
            cancel_open_orders(symbol)
            if is_rth():
                submit_market_sell(symbol, q)
            else:
                bbid = get_best_bid(symbol) or l
                lim = round_to_tick(bbid * 0.995)
                submit_limit("sell", symbol, q, lim, True)
            return

    # Timeout enforcement
    cancel_open_orders(symbol)
    if is_rth():
        submit_market_sell(symbol, qty)
        log(f"⚠️ Timeout → MARKET EXIT {symbol}")
    else:
        bbid = get_best_bid(symbol) or last
        lim = round_to_tick(bbid * 0.995)
        submit_limit("sell", symbol, qty, lim, True)
        log(f"⚠️ Timeout → FINAL LIMIT EXIT {symbol} @{lim}")

# ──────────────────────────────────────────────────────────────────────────────
# Panic Logic (manual or hard stop)
# ──────────────────────────────────────────────────────────────────────────────
def handle_panic(symbol):
    qty, _ = get_position(symbol)
    if qty <= 0:
        log(f"ℹ️ No position to panic-exit {symbol}")
        return
    cancel_open_orders(symbol)
    if is_rth():
        submit_market_sell(symbol, qty)
        log(f"🚨 PANIC EXIT {symbol} (market)")
    else:
        last = get_last_trade(symbol)
        bbid = get_best_bid(symbol) or last
        lim = round_to_tick(bbid * 0.99)
        submit_limit("sell", symbol, qty, lim, True)
        log(f"🚨 PANIC EXIT {symbol} (limit @{lim})")

# ──────────────────────────────────────────────────────────────────────────────
# Webhook endpoint
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/tv", methods=["POST"])
def tv():
    data = request.get_json(force=True)
    if not data or data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "forbidden"}), 403

    action = str(data.get("action", "")).upper()
    symbol = str(data.get("ticker", "")).upper()
    vwap   = float(data.get("vwap", 0) or 0)
    upper  = float(data.get("upper_band", 0) or 0)
    mama   = float(data.get("mama", 0) or 0)

    log(f"🚀 Alert Received:\n{json.dumps(data, indent=2)}")

    if "BUY" in action:
        handle_buy(symbol, vwap, upper, mama, action)
    elif action == "EXIT_SIGNAL":
        handle_exit(symbol, upper, mama)
    elif action == "PANIC":
        handle_panic(symbol)
    else:
        log(f"⚠️ Unknown action {action}")

    return jsonify({"status": "ok"}), 200

# ──────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)






















