import os
import json
import threading
from flask import Flask, request, jsonify
import requests

app = Flask(__name__)

# ======================= ENV VARIABLES =======================

APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

# ======================= ALPACA CONFIG ========================

HEADERS = {
    "APCA-API-KEY-ID": APCA_API_KEY_ID,
    "APCA-API-SECRET-KEY": APCA_API_SECRET_KEY,
    "Content-Type": "application/json"
}

ORDERS_URL = f"{APCA_API_BASE_URL}/v2/orders"
POSITIONS_URL = f"{APCA_API_BASE_URL}/v2/positions"

# ======================= LOCK =======================

trade_lock = threading.Lock()

# ======================= HELPERS =======================

def get_positions():
    try:
        r = requests.get(POSITIONS_URL, headers=HEADERS)
        if r.status_code == 200:
            return r.json()
        return []
    except Exception as e:
        print("POSITION ERROR:", str(e))
        return []


def position_exists(symbol):
    positions = get_positions()
    for pos in positions:
        if pos["symbol"] == symbol:
            return True
    return False


def place_limit_order(symbol, qty, side, limit_price):

    order_data = {
        "symbol": symbol,
        "qty": int(qty),
        "side": side,
        "type": "limit",
        "limit_price": str(limit_price),
        "time_in_force": "gtc"
    }

    print("ORDER PAYLOAD:", order_data)

    r = requests.post(ORDERS_URL, json=order_data, headers=HEADERS)

    print("ALPACA RESPONSE:", r.status_code, r.text)

    return r.status_code, r.text

# ======================= WEBHOOK =======================

@app.route("/", methods=["GET"])
def health():
    return "Bot is live", 200


@app.route("/tv", methods=["POST"])
def tradingview_webhook():

    try:
        data = request.get_json(force=True)
        print("WEBHOOK RECEIVED:", data)

    except Exception as e:
        return jsonify({"error": "Invalid JSON", "details": str(e)}), 400

    # Security check
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    action = data.get("action")
    symbol = data.get("ticker")
    qty = data.get("quantity")
    entry = data.get("entry")

    if not all([action, symbol, qty, entry]):
        return jsonify({"error": "Missing fields"}), 400

    with trade_lock:

        if action == "PLAN" or action == "BUY":

            if position_exists(symbol):
                return jsonify({"msg": f"Position already open for {symbol}"}), 200

            status, msg = place_limit_order(symbol, qty, "buy", entry)

            return jsonify({"alpaca_status": status, "response": msg})


        elif action == "SELL":

            if not position_exists(symbol):
                return jsonify({"msg": f"No open position for {symbol}"}), 200

            status, msg = place_limit_order(symbol, qty, "sell", entry)

            return jsonify({"alpaca_status": status, "response": msg})

        else:
            return jsonify({"error": "Invalid action"}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)


















































































































