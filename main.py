import os
import threading
import asyncio
from flask import Flask, request, jsonify
import requests
from alpaca.data.live import StockDataStream

########################
# CONFIG
########################
STEP = 0.01               # 0.01 above AND below $1 (your final decision)
MAX_BUY_LADDER = 6
MAX_SELL_LADDER = 6

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

app = Flask(__name__)

trade_lock = threading.Lock()
active_trade = None


########################
# CORE HELPERS (SYNC)
########################

def safe_request(method, url, **kwargs):
    try:
        return requests.request(method, url, headers=HEADERS, timeout=5, **kwargs)
    except Exception as e:
        print("REQUEST ERROR:", e)
        return None


def place_limit(symbol, qty, side, price):
    order = {
        "symbol": symbol,
        "qty": int(qty),
        "side": side,
        "type": "limit",
        "limit_price": str(round(float(price), 4)),
        "time_in_force": "day",
        "extended_hours": True
    }

    print(f"📤 {side.upper()} → {symbol} @ {price}")

    r = safe_request("POST", ORDERS_URL, json=order)

    if r is None:
        return None

    try:
        return r.json()
    except:
        return None


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
    if pos and float(pos["qty"]) > 0:
        return True
    return False


def get_order(order_id):
    r = safe_request("GET", f"{ORDERS_URL}/{order_id}")
    if r and r.status_code == 200:
        return r.json()
    return None


########################
# LADDER BUY (UP)
########################

async def ladder_buy(trade):
    symbol = trade["symbol"]
    base_price = trade["entry"]
    qty = trade["qty"]

    print(f"🪜 LADDER BUY STARTED: {symbol}")

    for i in range(MAX_BUY_LADDER):
        if await asyncio.to_thread(has_position, symbol):
            print("✅ BUY FILLED")
            return True

        price = round(base_price + (STEP * i), 4)
        print(f"BUY ATTEMPT {i+1} @ {price}")
        await asyncio.to_thread(place_limit, symbol, qty, "buy", price)

        await asyncio.sleep(2)

    print("❌ BUY NOT FILLED")
    return False


########################
# LADDER SELL (DOWN)
########################

async def ladder_sell(symbol, stop_price):
    print("🪜 LADDER EXIT STARTED")

    for i in range(MAX_SELL_LADDER):

        pos = await asyncio.to_thread(get_position, symbol)
        if not pos:
            print("✅ POSITION CLOSED")
            return

        qty = float(pos["qty"])

        price = round(stop_price - (STEP * i), 4)
        if price < 0:
            price = stop_price

        print(f"SELL ATTEMPT {i+1} @ {price}")

        await asyncio.to_thread(place_limit, symbol, qty, "sell", price)

        await asyncio.sleep(2)

    print("❌ EXIT FAILED — MANUAL CHECK NEEDED")


########################
# WEBSOCKET MONITOR
########################

def start_monitor(trade):
    global active_trade

    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()

    async def runner():
        try:
            symbol = trade["symbol"]
            stop = trade["stop"]
            target = trade["target"]

            print(f"📡 MONITORING {symbol}")

            stream = StockDataStream(API_KEY, SECRET_KEY, feed="sip")

            filled = await ladder_buy(trade)

            if not filled:
                return

            print("👀 WATCHING FOR STOP/TARGET")

            async def on_trade(data):
                price = float(data.price)

                if price <= stop:
                    print("🛑 STOP HIT")
                    await ladder_sell(symbol, stop)
                    await stream.stop()

                if price >= target:
                    print("🎯 TARGET HIT")
                    await ladder_sell(symbol, price)
                    await stream.stop()

            stream.subscribe_trades(on_trade, symbol)
            await stream.run()

        finally:
            with trade_lock:
                print("🧹 RESETTING ACTIVE TRADE")
                active_trade = None

    loop.run_until_complete(runner())


########################
# FLASK ENDPOINT
########################

@app.route("/tv", methods=["POST"])
def webhook():
    global active_trade

    data = request.json
    print("WEBHOOK:", data)

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Bad Secret"}), 403

    try:
        symbol = data["ticker"]
        qty = int(data["quantity"])
        entry = float(data["entry"])
        stop = float(data["stop"])
        target = float(data["target"])
    except:
        return jsonify({"error": "Invalid data"}), 400

    with trade_lock:
        if active_trade is not None:
            return jsonify({"error": "Trade running"}), 429

        active_trade = {
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "stop": stop,
            "target": target
        }

        threading.Thread(
            target=start_monitor,
            args=(active_trade,),
            daemon=True
        ).start()

    return jsonify({"status": f"{symbol} monitoring started"}), 200


@app.route("/", methods=["GET"])
def home():
    return "✅ ChrisBot Online"


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

























































































































