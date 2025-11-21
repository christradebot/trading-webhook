import os
import time
import threading
from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from alpaca.trading.requests import LimitOrderRequest
from alpaca.data import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# =============================
# CONFIG (from Railway)
# =============================

APCA_API_KEY_ID = os.environ.get("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
APCA_API_BASE_URL = os.environ.get("APCA_API_BASE_URL", "https://api.alpaca.markets")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
    raise Exception("Missing Alpaca API credentials in environment variables.")

# Live trading client (NOT paper)
trading_client = TradingClient(
    api_key=APCA_API_KEY_ID,
    secret_key=APCA_API_SECRET_KEY,
    paper=False
)

data_client = StockHistoricalDataClient(
    api_key=APCA_API_KEY_ID,
    secret_key=APCA_API_SECRET_KEY
)

app = Flask(__name__)

# Prevent duplicate entries
open_trade_symbols = set()


# =============================
# UTILS
# =============================

def get_latest_price(symbol: str) -> float:
    req = StockLatestTradeRequest(symbol_or_symbols=[symbol])
    trade = data_client.get_stock_latest_trade(req)[symbol]
    return float(trade.price)


def has_open_position(symbol: str) -> bool:
    try:
        positions = trading_client.get_all_positions()
        for p in positions:
            if p.symbol == symbol and float(p.qty) > 0:
                return True
        return False
    except Exception:
        return False


# =============================
# OPTION A ENTRY LADDER
# =============================

def option_a_entry(symbol, qty, entry_price):

    print(f"[ENTRY] Option A started for {symbol}")

    increments = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]

    for inc in increments:
        price = round(entry_price + inc, 2)

        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            limit_price=price
        )

        trading_client.submit_order(order)
        print(f"[ENTRY ATTEMPT] {symbol} @ ${price}")

        time.sleep(5)

        if has_open_position(symbol):
            print(f"[FILLED] {symbol} position opened.")
            return

    print(f"[MISSED] Entry missed for {symbol}")
    if symbol in open_trade_symbols:
        open_trade_symbols.remove(symbol)


# =============================
# OPTION A EXIT LADDER
# =============================

def option_a_exit(symbol, qty, stop_price):

    offsets = [0, -0.02, -0.05, -0.10]

    for offset in offsets:
        price = round(stop_price + offset, 2)

        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            type=OrderType.LIMIT,
            time_in_force=TimeInForce.GTC,
            limit_price=price
        )

        trading_client.submit_order(order)
        print(f"[EXIT ATTEMPT] {symbol} @ ${price}")

        time.sleep(5)

        if not has_open_position(symbol):
            print(f"[CLOSED] {symbol}")
            open_trade_symbols.discard(symbol)
            return


# =============================
# WEBHOOK HANDLER
# =============================

@app.route("/webhook", methods=["POST"])
def webhook():

    data = request.get_json()

    if not data:
        return jsonify({"error": "No data"}), 400

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Invalid secret"}), 403

    action = data.get("action")
    symbol = data.get("ticker", "").upper()
    qty = int(data.get("quantity", 1))
    entry = float(data.get("entry", 0))
    stop = float(data.get("stop", 0))

    # SAFETY CHECKS
    if not symbol or qty <= 0:
        return jsonify({"error": "Bad request"}), 400

    if symbol in open_trade_symbols:
        return jsonify({"status": f"{symbol} already processing"})

    if has_open_position(symbol):
        return jsonify({"status": f"{symbol} already has position"})

    # ================= BUY =================
    if action == "BUY":

        open_trade_symbols.add(symbol)

        thread = threading.Thread(
            target=option_a_entry,
            args=(symbol, qty, entry)
        )
        thread.start()

        return jsonify({"status": "BUY ladder started"})

    # ================= SELL =================
    if action == "SELL":

        if not has_open_position(symbol):
            return jsonify({"status": "No open position"})

        thread = threading.Thread(
            target=option_a_exit,
            args=(symbol, qty, stop)
        )
        thread.start()

        return jsonify({"status": "SELL ladder started"})

    return jsonify({"error": "Invalid action"}), 400


@app.route("/")
def home():
    return "Chris Trading Bot - LIVE OK ✅"














































































































