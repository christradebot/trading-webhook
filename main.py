import os
import time
import threading
from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from alpaca.trading.requests import LimitOrderRequest

# =============================
# ENV VARIABLES (Railway)
# =============================
APCA_API_KEY_ID     = os.environ.get("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
APCA_API_BASE_URL   = os.environ.get("APCA_API_BASE_URL", "https://api.alpaca.markets")
WEBHOOK_SECRET      = os.environ.get("WEBHOOK_SECRET")

if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY or not WEBHOOK_SECRET:
    raise Exception("🚨 Missing environment variables in Railway")

# =============================
# ALPACA CLIENT (LIVE)
# =============================
trading_client = TradingClient(
    api_key=APCA_API_KEY_ID,
    secret_key=APCA_API_SECRET_KEY,
    paper=False
)

# =============================
# APP
# =============================
app = Flask(__name__)

# Prevent duplicate trades
open_trade_symbols = set()

# =============================
# HELPERS
# =============================

def has_open_position(symbol: str) -> bool:
    try:
        positions = trading_client.get_all_positions()
        for p in positions:
            if p.symbol == symbol and float(p.qty) > 0:
                return True
        return False
    except Exception as e:
        print(f"[POSITION CHECK ERROR] {e}")
        return False


# =============================
# ENTRY LADDER (Option A)
# =============================

def option_a_entry(symbol, qty, entry_price):

    print(f"[ENTRY] Starting ladder for {symbol}")

    increments = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06]

    for inc in increments:

        price = round(entry_price + inc, 2)

        try:
            order = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                limit_price=price
            )

            trading_client.submit_order(order)
            print(f"[BUY ATTEMPT] {symbol} @ {price}")

        except Exception as e:
            print(f"[BUY ERROR] {e}")

        time.sleep(5)

        if has_open_position(symbol):
            print(f"[FILLED] {symbol} position opened")
            return

    print(f"[MISSED] Entry failed for {symbol}")
    open_trade_symbols.discard(symbol)


# =============================
# EXIT LADDER (Option A)
# =============================

def option_a_exit(symbol, qty, stop_price):

    print(f"[EXIT] Starting ladder for {symbol}")

    offsets = [0, -0.02, -0.05, -0.10]

    for offset in offsets:

        price = round(stop_price + offset, 2)

        try:
            order = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                type=OrderType.LIMIT,
                time_in_force=TimeInForce.GTC,
                limit_price=price
            )

            trading_client.submit_order(order)
            print(f"[SELL ATTEMPT] {symbol} @ {price}")

        except Exception as e:
            print(f"[SELL ERROR] {e}")

        time.sleep(5)

        if not has_open_position(symbol):
            print(f"[CLOSED] {symbol}")
            open_trade_symbols.discard(symbol)
            return


# =========================================================
# ✅ THIS MATCHES YOUR TRADINGVIEW:  /tv
# =========================================================

@app.route("/tv", methods=["POST"])
def tradingview_webhook():

    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400

    # ✅ SECRET CHECK
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Invalid secret"}), 403

    # ✅ PAYLOAD MATCH
    action  = data.get("action")
    symbol  = data.get("ticker", "").upper()
    qty     = int(data.get("quantity", 1))
    entry   = float(data.get("entry", 0))
    stop    = float(data.get("stop", 0))

    print(f"[WEBHOOK] {action} {symbol} Qty:{qty} Entry:{entry} Stop:{stop}")

    if not symbol or qty <= 0:
        return jsonify({"error": "Invalid input"}), 400

    # ================= SAFETY
    if symbol in open_trade_symbols:
        return jsonify({"status": f"{symbol} already processing"})

    # ================= BUY
    if action == "BUY":

        if has_open_position(symbol):
            return jsonify({"status": f"{symbol} already has position"})

        open_trade_symbols.add(symbol)

        threading.Thread(
            target=option_a_entry,
            args=(symbol, qty, entry),
            daemon=True
        ).start()

        return jsonify({"status": "BUY ladder started ✅"})

    # ================= SELL
    if action == "SELL":

        if not has_open_position(symbol):
            return jsonify({"status": "No position to sell"})

        threading.Thread(
            target=option_a_exit,
            args=(symbol, qty, stop),
            daemon=True
        ).start()

        return jsonify({"status": "SELL ladder started ✅"})

    return jsonify({"error": "Invalid action"}), 400


# =============================
# HEALTH CHECK
# =============================

@app.route("/")
def home():
    return "Chris Trading Bot - LIVE ✅"

















































































































