import os
import threading
import time
from flask import Flask, request, jsonify
import requests

from alpaca.data.live import StockDataStream

app = Flask(__name__)

# ====================== ENV ======================

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

ORDERS_URL = f"{BASE_URL}/v2/orders"
POSITIONS_URL = f"{BASE_URL}/v2/positions"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json"
}

trade_lock = threading.Lock()
active_trade = None


# ====================== HELPERS ======================

def get_positions():
    try:
        r = requests.get(POSITIONS_URL, headers=HEADERS, timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("POSITION ERROR:", str(e))
    return []


def position_exists(symbol):
    for pos in get_positions():
        if pos["symbol"] == symbol:
            return True
    return False


def place_limit_order(symbol, qty, side, price):
    order = {
        "symbol": symbol,
        "qty": int(qty),
        "side": side,
        "type": "limit",
        "limit_price": str(round(float(price), 4)),

        # ✅ REQUIRED FOR PRE/POST MARKET
        "time_in_force": "day",
        "extended_hours": True
    }

    print("SENDING ORDER:", order)

    r = requests.post(ORDERS_URL, json=order, headers=HEADERS)

    print("ALPACA:", r.status_code, r.text)
    return r.status_code in [200, 201]


# ====================== LADDER EXIT ======================

def ladder_exit(symbol, qty, start_price):
    print("🪜 LADDER EXIT STARTED")

    price = round(float(start_price), 4)

    for i in range(6):  # 30 seconds total
        print(f"ATTEMPT {i + 1} @ {price}")

        place_limit_order(symbol, qty, "sell", price)
        time.sleep(5)

        if not position_exists(symbol):
            print("✅ POSITION CLOSED")
            return

        price = round(price - 0.01, 4)

    print("⚠️ FINAL AGGRESSIVE EXIT")
    place_limit_order(symbol, qty, "sell", price)


# ====================== WEBSOCKET ENGINE ======================

def start_websocket(trade):
    print(f"📡 WEBSOCKET STARTED FOR: {trade['symbol']}")

    highest_price = trade["entry"]
    trail_active = False

    # ✅ SIP feed (you paid for this)
    stream = StockDataStream(API_KEY, SECRET_KEY, feed="sip")

    async def on_trade(data):
        nonlocal highest_price, trail_active

        price = float(data.price)
        symbol = trade["symbol"]

        print(f"LIVE {symbol} : {price}")

        # Track highest price
        if price > highest_price:
            highest_price = price

        # Activate trailing after +20%
        if not trail_active and price >= trade["entry"] * 1.20:
            trail_active = True
            print("✅ TRAILING ACTIVATED")

        stop = trade["stop"]
        target = trade["target"]

        # Adjust stop once trailing is active
        if trail_active:
            stop = round(highest_price * (1 - trade["trail"] / 100), 4)

        print(f"STOP: {stop} | TARGET: {target}")

        # Touch-based exit
        if price <= stop or price >= target:
            print("🚨 EXIT CONDITION HIT")

            ladder_exit(symbol, trade["qty"], price)

            await stream.stop()

    stream.subscribe_trades(on_trade, trade["symbol"])
    stream.run()


# ====================== FLASK ======================

@app.route("/", methods=["GET"])
def health():
    return "Bot is live ✅", 200


@app.route("/tv", methods=["POST"])
def tradingview_webhook():
    global active_trade

    try:
        data = request.get_json(force=True)
        print("WEBHOOK:", data)
    except Exception as e:
        return jsonify({"error": "Bad JSON", "details": str(e)}), 400

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    symbol = data.get("ticker")
    qty = data.get("quantity")
    entry = float(data.get("entry"))
    stop = float(data.get("stop"))
    target = float(data.get("target"))
    trail = float(data.get("trail", 15))

    if not all([symbol, qty, entry, stop, target]):
        return jsonify({"error": "Missing fields"}), 400

    if position_exists(symbol):
        return jsonify({"msg": f"Position already exists for {symbol}"}), 200

    with trade_lock:

        ok = place_limit_order(symbol, qty, "buy", entry)

        if not ok:
            return jsonify({"error": "Buy order failed"}), 500

        active_trade = {
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "stop": stop,
            "target": target,
            "trail": trail
        }

        ws_thread = threading.Thread(target=start_websocket, args=(active_trade,))
        ws_thread.daemon = True
        ws_thread.start()

    return jsonify({"msg": f"{symbol} trade live & monitored"}), 200


if __name__ == "__main__":
    print("USING BASE URL:", BASE_URL)
    print("API KEY STATUS:", "SET ✅" if API_KEY else "MISSING ❌")
    app.run(host="0.0.0.0", port=8080)





















































































































