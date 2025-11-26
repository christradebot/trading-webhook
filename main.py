import os
import threading
import asyncio
from flask import Flask, request, jsonify
import requests
from alpaca.data.live import StockDataStream

app = Flask(__name__)

# ===================== ENV ======================

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

ORDERS_URL = f"{BASE_URL}/v2/orders"
POSITIONS_URL = f"{BASE_URL}/v2/positions"
ACCOUNT_URL = f"{BASE_URL}/v2/account"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json"
}

trade_lock = threading.Lock()
active_trade = None

# ===================== HELPERS ======================

def safe_request(method, url, **kwargs):
    try:
        r = requests.request(method, url, headers=HEADERS, timeout=10, **kwargs)
        return r
    except Exception as e:
        print(f"REQUEST ERROR [{url}]: {e}")
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

    print("📤 SENDING:", order)

    r = safe_request("POST", ORDERS_URL, json=order)

    if r is None:
        return None, 500

    print("📥 ALPACA:", r.status_code, r.text)

    try:
        return r.json(), r.status_code
    except:
        return {}, r.status_code


def get_position(symbol):
    r = safe_request("GET", POSITIONS_URL)

    if r is None or r.status_code != 200:
        return None

    for p in r.json():
        if p["symbol"] == symbol:
            return p

    return None


def has_position(symbol):
    return get_position(symbol) is not None


def get_order_status(order_id):
    r = safe_request("GET", f"{ORDERS_URL}/{order_id}")
    if r and r.status_code == 200:
        return r.json()
    return None


# ===================== FILL CHECK ======================

async def wait_for_fill(symbol, order_id):
    print(f"⏳ Waiting for {symbol} to fill...")

    while True:

        order_status = await asyncio.to_thread(get_order_status, order_id)

        # If order was killed
        if order_status and order_status["status"] in ["rejected", "canceled", "expired"]:
            print(f"❌ Order {order_status['status'].upper()} — EXITING THREAD")
            return None, None

        pos = await asyncio.to_thread(get_position, symbol)

        if pos:
            print(f"✅ {symbol} FILLED @ {pos['avg_entry_price']}")
            return float(pos["avg_entry_price"]), float(pos["qty"])

        await asyncio.sleep(2)


# ====================== LADDER EXIT ======================

async def ladder_exit(symbol, start_price, hard_stop):

    position = await asyncio.to_thread(get_position, symbol)

    if not position:
        print("ℹ️ No position to sell")
        return

    qty = float(position["qty"])
    price = float(start_price)

    print(f"🪜 STARTING LADDER - QTY: {qty}")

    for i in range(6):

        # Never go below hard stop
        if price < hard_stop:
            price = hard_stop

        print(f"ATTEMPT {i+1} @ {price}")

        await asyncio.to_thread(place_limit_order, symbol, qty, "sell", price)
        await asyncio.sleep(5)

        if not await asyncio.to_thread(has_position, symbol):
            print("✅ POSITION CLOSED")
            return

        price = round(price - 0.01, 4)

    print("⚠️ FINAL EXIT ATTEMPT")
    await asyncio.to_thread(place_limit_order, symbol, qty, "sell", price)


# ====================== WEBSOCKET ======================

def start_websocket(trade):

    global active_trade

    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()

    async def runner():

        try:
            print(f"📡 SOCKET STARTED FOR {trade['symbol']}")

            stream = StockDataStream(API_KEY, SECRET_KEY, feed="sip")

            entry_price, qty = await wait_for_fill(
                trade["symbol"],
                trade["order_id"]
            )

            if entry_price is None:
                print("❌ Fill never happened — exiting monitor")
                return

            highest = entry_price
            trail_active = False

            async def on_trade(data):
                nonlocal highest, trail_active

                price = float(data.price)
                symbol = trade["symbol"]

                if price > highest:
                    highest = price

                # Activate trail after +20%
                if not trail_active and price >= entry_price * 1.2:
                    trail_active = True
                    print("🔥 TRAILING ACTIVATED")

                stop = trade["stop"]
                target = trade["target"]

                if trail_active:
                    trail_stop = round(highest * (1 - trade["trail"] / 100), 4)
                    stop = max(stop, trail_stop)

                print(f"{symbol} | {price} | STOP: {stop} | TARGET: {target}")

                if price <= stop or price >= target:
                    print("🚨 EXIT TRIGGERED")
                    await ladder_exit(symbol, price, trade["stop"])
                    await stream.stop()

            stream.subscribe_trades(on_trade, trade["symbol"])
            await stream._run_forever()

        finally:
            print("🧹 CLEANING ACTIVE TRADE")
            active_trade = None

    loop.run_until_complete(runner())


# ====================== FLASK ======================

@app.route("/")
def health():
    return "Bot Online ✅", 200


@app.route("/tv", methods=["POST"])
def webhook():

    global active_trade

    data = request.get_json(force=True)
    print("WEBHOOK:", data)

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    symbol = data["ticker"]
    qty    = data["quantity"]
    entry  = float(data["entry"])
    stop   = float(data["stop"])
    target = float(data["target"])
    trail  = float(data.get("trail", 15))

    with trade_lock:

        if active_trade is not None:
            return jsonify({"error": "Trade already running"}), 400

        order_json, status = place_limit_order(symbol, qty, "buy", entry)

        if not order_json or "id" not in order_json:
            return jsonify({"error": "Buy order rejected"}), 500

        active_trade = {
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "stop": stop,
            "target": target,
            "trail": trail,
            "order_id": order_json["id"]
        }

        t = threading.Thread(target=start_websocket, args=(active_trade,))
        t.daemon = True
        t.start()

    return jsonify({"msg": f"{symbol} monitoring started ✅"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)























































































































