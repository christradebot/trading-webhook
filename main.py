from flask import Flask, request, jsonify
import os
import time

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from alpaca.data.live import StockDataStream

# -----------------------------------
# ENV
# -----------------------------------

API_KEY = os.environ.get("ALPACA_API_KEY")
API_SECRET = os.environ.get("ALPACA_API_SECRET")
BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

if not API_KEY or not API_SECRET:
    raise Exception("Missing Alpaca API keys")

# -----------------------------------
# CLIENTS
# -----------------------------------

trading_client = TradingClient(API_KEY, API_SECRET, paper=True)

app = Flask(__name__)

active_trades = {}
last_order_time = {}

# -----------------------------------
# SAFETY
# -----------------------------------

COOLDOWN_SECONDS = 30   # no duplicate orders within 30s

def in_cooldown(symbol):
    last_time = last_order_time.get(symbol)
    if not last_time:
        return False
    return (time.time() - last_time) < COOLDOWN_SECONDS


def position_exists(symbol):
    positions = trading_client.get_all_positions()
    for pos in positions:
        if pos.symbol == symbol and float(pos.qty) > 0:
            return True
    return False


# -----------------------------------
# PRICE
# -----------------------------------

def get_last_price(symbol):
    from alpaca.data import StockHistoricalDataClient
    from alpaca.data.requests import StockLatestTradeRequest

    data_client = StockHistoricalDataClient(API_KEY, API_SECRET)

    request_params = StockLatestTradeRequest(symbol_or_symbols=symbol)
    latest_trade = data_client.get_stock_latest_trade(request_params)

    if symbol in latest_trade:
        return float(latest_trade[symbol].price)

    return None


# -----------------------------------
# WEBHOOK
# -----------------------------------

@app.route("/", methods=["GET"])
def home():
    return "Bot is alive"


@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json()

    if not data:
        return jsonify({"error": "No JSON received"}), 400

    secret = data.get("secret")

    if secret != os.environ.get("WEBHOOK_SECRET"):
        return jsonify({"error": "Unauthorized"}), 403

    symbol = data.get("ticker").upper()
    qty = int(data.get("quantity", 1))
    entry = float(data.get("entry"))
    stop = float(data.get("stop"))
    target = float(data.get("target"))

    print(f"[WEBHOOK] Received for {symbol}")

    if in_cooldown(symbol):
        return jsonify({"status": "Cooldown active, ignored"}), 200

    if position_exists(symbol):
        return jsonify({"status": "Position already exists"}), 200

    last_price = get_last_price(symbol)

    if not last_price:
        return jsonify({"error": "No price available"}), 500

    print(f"[PRICE] {symbol} = {last_price}")

    if abs(last_price - entry) > (entry * 0.10):
        print("[SAFETY] Too far from entry. Aborted.")
        return jsonify({"status": "Price outside safe range"}), 200

    # ----- LIMIT LADDER (OPTION A) -----

    price_steps = [0, 0.01, 0.02, 0.03, 0.04, 0.05]

    for i, offset in enumerate(price_steps):
        limit_price = round(entry + offset, 2)

        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            limit_price=limit_price,
            time_in_force=TimeInForce.GTC
        )

        print(f"[TRY {i+1}] BUY {qty} @ {limit_price}")

        trading_client.submit_order(order)

        time.sleep(5)

        if position_exists(symbol):
            last_order_time[symbol] = time.time()
            print("[SUCCESS] Position opened")
            return jsonify({"status": "Bought", "price": limit_price}), 200

    print("[FAILED] Entry not filled")
    last_order_time[symbol] = time.time()

    return jsonify({"status": "Missed entry - protected capital"}), 200












































































































