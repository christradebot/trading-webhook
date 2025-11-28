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
        return requests.request(method, url, headers=HEADERS, timeout=5, **kwargs)
    except:
        return None


def place_limit_order(symbol, qty, side, price):
    qty = int(qty)

    order = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "limit",
        "limit_price": str(round(float(price), 4)),
        "time_in_force": "day",
        "extended_hours": True
    }

    print(f"📤 {side.upper()} ORDER → {symbol} @ {price}")
    r = safe_request("POST", ORDERS_URL, json=order)

    if r is None:
        return None, 500

    try:
        return r.json(), r.status_code
    except:
        return {"error": r.text}, r.status_code


def get_position(symbol):
    r = safe_request("GET", POSITIONS_URL)
    if not r or r.status_code != 200:
        return None
    for p in r.json():
        if p["symbol"] == symbol:
            return p
    return None


def has_position(symbol):
    pos = get_position(symbol)
    return pos and float(pos.get("qty", 0)) > 0


def get_order_status(order_id):
    r = safe_request("GET", f"{ORDERS_URL}/{order_id}")
    return r.json() if r and r.status_code == 200 else None


# ===================== WAIT FOR FILL ======================

async def wait_for_fill(symbol, order_id):
    print(f"⏳ Waiting for {symbol} fill...")

    while True:
        order = await asyncio.to_thread(get_order_status, order_id)

        if order and order.get("status") in ["rejected", "canceled", "expired"]:
            print(f"❌ ORDER {order['status'].upper()}")
            return None, None

        pos = await asyncio.to_thread(get_position, symbol)

        if pos and float(pos["qty"]) > 0:
            entry = float(pos["avg_entry_price"])
            qty = float(pos["qty"])
            print(f"✅ BUY FILLED → {symbol} @ {entry}")
            print(f"👀 Monitoring STOP: {active_trade['stop']} | TARGET: {active_trade['target']}")
            return entry, qty

        await asyncio.sleep(2)


# ===================== LADDER EXIT ======================

async def ladder_exit(symbol, start_price, hard_stop):
    print("🪜 Ladder exit started")

    price = float(start_price)

    for _ in range(6):
        pos = await asyncio.to_thread(get_position, symbol)

        if not pos or float(pos["qty"]) <= 0:
            print("✅ POSITION CLOSED")
            return

        qty_to_sell = float(pos["qty"])
        price = max(price, hard_stop)

        print(f"🔴 EXIT ATTEMPT → {symbol} @ {round(price,4)}")

        await asyncio.to_thread(place_limit_order, symbol, qty_to_sell, "sell", price)
        await asyncio.sleep(5)

        if not await asyncio.to_thread(has_position, symbol):
            print("✅ POSITION CLOSED")
            return

        price = round(price - 0.01, 4)

    print("⚠️ Final exit attempt")
    final_pos = await asyncio.to_thread(get_position, symbol)

    if final_pos and float(final_pos["qty"]) > 0:
        await asyncio.to_thread(place_limit_order, symbol, final_pos["qty"], "sell", price)


# ===================== WEBSOCKET ======================

def start_websocket(trade):
    global active_trade

    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()

    async def runner():
        try:
            stream = StockDataStream(API_KEY, SECRET_KEY, feed="sip")

            entry_price, _ = await wait_for_fill(trade["symbol"], trade["order_id"])
            if entry_price is None:
                return

            highest = entry_price
            trail_active = False

            async def on_trade(data):
                nonlocal highest, trail_active

                price = float(data.price)
                symbol = trade["symbol"]

                if price > highest:
                    highest = price

                if not trail_active and price >= entry_price * 1.2:
                    trail_active = True
                    print("🔥 Trailing activated")

                stop = trade["stop"]
                target = trade["target"]

                if trail_active:
                    trail_stop = round(highest * (1 - trade["trail"] / 100), 4)
                    stop = max(stop, trail_stop)

                print(f"📈 {symbol} → {price}")

                if price <= stop:
                    print("🚨 STOP HIT")
                    await ladder_exit(symbol, price, trade["stop"])
                    await stream.stop()

                if price >= target:
                    print("🎯 TARGET HIT")
                    await ladder_exit(symbol, price, trade["stop"])
                    await stream.stop()

            stream.subscribe_trades(on_trade, trade["symbol"])
            await stream.run()

        except Exception as e:
            print(f"CRITICAL ERROR: {str(e)}")

        finally:
            with trade_lock:
                print("🧹 ACTIVE TRADE CLEARED")
                active_trade = None

    loop.run_until_complete(runner())


# ===================== FLASK ======================

@app.route("/")
def health():
    return "Bot Online ✅", 200


@app.route("/tv", methods=["POST"])
def webhook():
    global active_trade

    data = request.get_json(force=True)
    print(f"✅ SIGNAL RECEIVED: {data}")

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    try:
        symbol = data["ticker"]
        qty = int(data["quantity"])
        entry = float(data["entry"])
        stop = float(data["stop"])
        target = float(data["target"])
        trail = float(data.get("trail", 15))
    except:
        return jsonify({"error": "Invalid payload"}), 400

    with trade_lock:
        if active_trade:
            return jsonify({"error": "Trade already running"}), 429

        order_json, status = place_limit_order(symbol, qty, "buy", entry)

        if status not in [200, 201] or "id" not in order_json:
            return jsonify({"error": "Buy rejected", "alpaca": order_json}), 500

        active_trade = {
            "symbol": symbol,
            "entry": entry,
            "stop": stop,
            "target": target,
            "trail": trail,
            "order_id": order_json["id"]
        }

        t = threading.Thread(target=start_websocket, args=(active_trade,), daemon=True)
        t.start()

    return jsonify({"msg": f"{symbol} BUY SENT - Monitoring started"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)


























































































































