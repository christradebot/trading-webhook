import os
import time
import json
from flask import Flask, request, jsonify
from datetime import datetime

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# =========================
# CONFIG
# =========================

ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
PAPER = os.getenv("PAPER", "true").lower() == "true"

MAX_ATTEMPTS = 6
ENTRY_STEP = 0.01
EXIT_STEP = 0.02
SLEEP_SECONDS = 5

# =========================
# CLIENTS
# =========================

trading_client = TradingClient(ALPACA_KEY, ALPACA_SECRET, paper=PAPER)
data_client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)

# =========================
# APP
# =========================

app = Flask(__name__)

active_trades = {}

# =========================
# HELPERS
# =========================

def log(msg):
    print(f"[{datetime.now()}] {msg}", flush=True)


def get_latest_price(symbol):
    try:
        request = StockLatestTradeRequest(symbol_or_symbols=symbol)
        trade = data_client.get_stock_latest_trade(request)
        price = trade[symbol].price
        log(f"[PRICE] {symbol} latest price = {price}")
        return float(price)
    except Exception as e:
        log(f"[ERROR] Price fetch failed: {e}")
        return None


def has_open_position(symbol):
    try:
        position = trading_client.get_position(symbol)
        log(f"[POSITION] Open position exists for {symbol}")
        return True
    except:
        log(f"[POSITION] No open position for {symbol}")
        return False


def place_limit(side, symbol, qty, price):
    order = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=round(price, 2)
    )
    return trading_client.submit_order(order_data=order)


# =========================
# ENTRY LOGIC (OPTION A)
# =========================

def option_a_entry(symbol, qty, base_price):

    log(f"[ENTRY] Starting Option A ladder for {symbol}")

    for i in range(MAX_ATTEMPTS):

        limit_price = round(base_price + (i * ENTRY_STEP), 2)
        log(f"[ENTRY] Attempt {i+1} at ${limit_price}")

        place_limit("buy", symbol, qty, limit_price)
        time.sleep(SLEEP_SECONDS)

        if has_open_position(symbol):
            log(f"[ENTRY FILLED] at ${limit_price}")
            active_trades[symbol] = {
                "entry_price": limit_price,
                "qty": qty
            }
            return True

    log(f"[FAILED] Entry not filled after {MAX_ATTEMPTS} tries.")
    return False


# =========================
# EXIT LOGIC (OPTION A MODIFIED)
# =========================

def option_a_exit(symbol):

    if symbol not in active_trades:
        log(f"[EXIT] No tracked trade for {symbol}")
        return

    qty = active_trades[symbol]["qty"]
    exit_price = get_latest_price(symbol)

    if not exit_price:
        return

    log(f"[EXIT] Starting ladder exit")

    for i in range(MAX_ATTEMPTS):
        price = round(exit_price - (i * EXIT_STEP), 2)
        log(f"[EXIT] Attempt {i+1} at ${price}")
        place_limit("sell", symbol, qty, price)
        time.sleep(SLEEP_SECONDS)

        if not has_open_position(symbol):
            log(f"[EXIT FILLED] at ${price}")
            active_trades.pop(symbol)
            return

    log(f"[FORCED EXIT] aggressive limit")
    final_price = round(exit_price * 0.98, 2)
    place_limit("sell", symbol, qty, final_price)


# =========================
# WEBHOOK
# =========================

@app.route("/webhook", methods=["POST"])
def webhook():

    raw = request.data.decode("utf-8").strip()
    log(f"[WEBHOOK RAW] {raw}")

    try:
        payload = json.loads(raw)
    except:
        log("[ERROR] JSON decode failed")
        return jsonify({"status":"bad_json"}), 400

    secret = payload.get("secret")
    action = payload.get("action")
    symbol = payload.get("ticker")
    qty = int(payload.get("quantity", 1))
    entry = float(payload.get("entry", 0))

    if secret != os.getenv("WEBHOOK_SECRET"):
        return jsonify({"status":"unauthorized"}), 403

    if action == "ENTER":
        if has_open_position(symbol):
            return jsonify({"status":"already_in_trade"})

        if entry == 0:
            entry = get_latest_price(symbol)

        result = option_a_entry(symbol, qty, entry)
        return jsonify({"status": result})

    if action == "EXIT":
        option_a_exit(symbol)
        return jsonify({"status":"exit started"})

    return jsonify({"status":"unknown action"})


# =========================
# START
# =========================

if __name__ == "__main__":
    log("[ENGINE] Starting Trading Engine")
    app.run(host="0.0.0.0", port=8080)













































































































