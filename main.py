import os
import threading
import time
import asyncio
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
ORDER_STATUS_URL = f"{BASE_URL}/v2/orders/"

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


def get_position_qty(symbol):
    for pos in get_positions():
        if pos["symbol"] == symbol:
            return float(pos["qty"])
    return 0


def get_order_status(order_id):
    try:
        r = requests.get(f"{ORDER_STATUS_URL}{order_id}", headers=HEADERS, timeout=5)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print("ORDER STATUS ERROR:", str(e))
    return None


def place_limit_order(symbol, qty, side, price):
    order = {
        "symbol": symbol,
        "qty": int(qty),
        "side": side,
        "type": "limit",
        "limit_price": str(round(float(price), 4)),
        "time_in_force": "day",
        "extended_hours": True
    }

    print("SENDING ORDER:", order)
    r = requests.post(ORDERS_URL, json=order, headers=HEADERS)

    print("ALPACA:", r.status_code, r.text)

    if r.status_code in [200, 201]:
        return r.json().get("id")

    return None


# ====================== LADDER EXIT ======================

def ladder_exit(symbol, qty, start_price):
    print("🪜 LADDER EXIT STARTED")

    price = float(start_price)

    for i in range(6):  # 30 seconds total
        print(f"ATTEMPT {i + 1} @ {price}")

        place_limit_order(symbol, qty, "sell", price)
        time.sleep(5)

        if get_position_qty(symbol) <= 0:
            print("✅ POSITION CLOSED")
            return

        price = round(price - 0.01, 4)

    print("⚠️ FINAL EXIT ATTEMPT")
    place_limit_order(symbol, qty, "sell", price)


# ====================== WEBSOCKET ENGINE ======================

def start_websocket(trade, order_id):
    print(f"📡 WEBSOCKET STARTED FOR: {trade['symbol']}")

    highest_price = trade["entry"]
    trail_active = False
    entry_filled = False

    stream = StockDataStream(API_KEY, SECRET_KEY, feed="sip")

    async def on_trade(data):
        nonlocal highest_price, trail_active, entry_filled

        price = float(data.price)
        symbol = trade["symbol"]

        print(f"LIVE {symbol} : {price}")

        # ---- CHECK IF BUY HAS FILLED ----
        if not entry_filled:
            order_info = get_order_status(order_id)

            if order_info and order_info.get("status") == "filled":
                if get_position_qty(symbol) > 0:
                    entry_filled = True
                    print("✅ BUY ORDER FILLED — POSITION CONFIRMED")
                else:
                    return
            else:
                print("⌛ Waiting for BUY fill...")
                return

        # Track highest price after fill
        if price > highest_price:
            highest_price = price

        # Activate trailing after +20%
        if not trail_active and price >= trade["entry"] * 1.20:
            trail_active = True
            print("✅ TRAILING ACTIVATED")

        stop = trade["stop"]
        target = trade["target"]

        if trail_active:
            stop = round(highest_price * (1 - trade["trail"] / 100), 4)

        print(f"STOP: {stop} | TARGET: {target}")

        if price <= stop or price >= target:
            print("🚨 EXIT CONDITION HIT")

            if get_position_qty(symbol) > 0:
                ladder_exit(symbol, trade["qty"], price)
                await stream.stop()
            else:
                print("NO POSITION — SKIPPING EXIT")

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
    qty = int(data.get("quantity"))
    entry = float(data.get("entry"))
    stop = float(data.get("stop"))
    target = float(data.get("target"))
    trail = float(data.get("trail", 15))

    if not all([symbol, qty, entry, stop, target]):
        return jsonify({"error": "Missing fields"}), 400

    with trade_lock:

        order_id = place_limit_order(symbol, qty, "buy", entry)

        if not order_id:
            return jsonify({"error": "Buy order failed"}), 500

        active_trade = {
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "stop": stop,
            "target": target,
            "trail": trail
        }

        ws_thread = threading.Thread(target=start_websocket, args=(active_trade, order_id))
        ws_thread.daemon = True
        ws_thread.start()

    return jsonify({"msg": f"{symbol} trade live & monitored"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)






















































































































