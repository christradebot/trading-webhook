# === main.py ===
# © Chris / Athena 2025
# Main trading bot with ADD-on logic (Bullish Hammer rule)

from flask import Flask, request, jsonify
import os, json, time
from alpaca_trade_api.rest import REST, TimeFrame

app = Flask(__name__)

#────────────────────────────────────────────
# ENVIRONMENT
#────────────────────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "mysecret")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')

#────────────────────────────────────────────
# STATE MANAGEMENT
#────────────────────────────────────────────
open_positions = {}
add_done = {}  # Track whether we've already added once per symbol

#────────────────────────────────────────────
# HELPERS
#────────────────────────────────────────────
def log(msg): 
    print(time.strftime("[%H:%M:%S]"), msg)

def safe_qty(symbol):
    try:
        pos = api.get_position(symbol)
        return float(pos.qty)
    except Exception:
        return 0

def last_price(symbol):
    try:
        quote = api.get_latest_trade(symbol)
        return float(quote.price)
    except Exception:
        return 0

def in_profit(symbol):
    """Check if current price is above average entry price."""
    try:
        pos = api.get_position(symbol)
        current = float(api.get_latest_trade(symbol).price)
        avg_entry = float(pos.avg_entry_price)
        return current > avg_entry
    except Exception:
        return False

#────────────────────────────────────────────
# ENTRY / EXIT LOGIC
#────────────────────────────────────────────
def try_enter(symbol, qty, entry_price, signal_low, atr, signal_high):
    """Entry execution with limit order"""
    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="limit",
            time_in_force="day",
            limit_price=entry_price,
            extended_hours=True
        )
        open_positions[symbol] = {
            "entry": entry_price,
            "stop": signal_low,
            "atr": atr,
            "signal_high": signal_high,
            "added": False
        }
        log(f"✅ Entry order sent for {symbol} @ {entry_price}")
        return jsonify(status="entered"), 200
    except Exception as e:
        log(f"❌ Entry failed for {symbol}: {e}")
        return jsonify(error=str(e)), 500


def force_exit_until_flat(symbol):
    """Force exit until flat"""
    while True:
        qty = safe_qty(symbol)
        if qty <= 0:
            break
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="market",
            time_in_force="day"
        )
        time.sleep(1)
    open_positions.pop(symbol, None)
    add_done.pop(symbol, None)
    log(f"💥 {symbol} closed completely.")


#────────────────────────────────────────────
# ROUTE
#────────────────────────────────────────────
@app.post("/tv")
def tv():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403

    sym          = data.get("ticker")
    act          = data.get("action")
    qty          = int(data.get("quantity", 1))
    entry_price  = float(data.get("entry_price", 0))
    signal_low   = float(data.get("signal_low", 0))
    signal_high  = float(data.get("signal_high", 0))
    atr          = float(data.get("atr", 0))

    log(f"📨 {act} alert received for {sym}")

    #────────────────────────────────────────
    # BUY
    #────────────────────────────────────────
    if act == "BUY":
        return try_enter(sym, qty, entry_price, signal_low, atr, signal_high)

    #────────────────────────────────────────
    # ADD (Bullish Hammer Add-on)
    #────────────────────────────────────────
    if act == "ADD":
        if safe_qty(sym) <= 0:
            log(f"🚫 ADD skipped for {sym} — no open position.")
            return jsonify(status="no_position"), 200

        if add_done.get(sym, False):
            log(f"🚫 ADD skipped for {sym} — already added once.")
            return jsonify(status="add_skipped"), 200

        if not in_profit(sym):
            log(f"🚫 ADD skipped for {sym} — position not in profit.")
            return jsonify(status="no_profit"), 200

        log(f"➕ ADD triggered for {sym} (Bullish Hammer) @ {entry_price}")
        add_done[sym] = True
        return try_enter(sym, qty, entry_price, signal_low, atr, signal_high)

    #────────────────────────────────────────
    # EXIT
    #────────────────────────────────────────
    if act == "EXIT":
        force_exit_until_flat(sym)
        return jsonify(status="closed"), 200

    return jsonify(status="ok"), 200


#────────────────────────────────────────────
# TEST ROUTE
#────────────────────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, service="tv→alpaca", base=ALPACA_BASE_URL)





























