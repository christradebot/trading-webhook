import os
import threading
import asyncio
from flask import Flask, request, jsonify
import requests
from alpaca.data.live import StockDataStream

app = Flask(__name__)

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

ORDERS_URL = f"{BASE_URL}/v2/orders"
POSITIONS_URL = f"{BASE_URL}/v2/positions"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY
}

trade_lock = threading.Lock()
active_trade = None
stream = None
stream_thread = None

def place_limit_order(symbol, qty, side, price):
    data = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "limit",
        "limit_price": price,
        "time_in_force": "day",
        "extended_hours": True
    }

    print(f"[ORDER] {side.upper()} {qty} {symbol} @ {price}")
    r = requests.post(ORDERS_URL, json=data, headers=HEADERS)
    print("[ALPACA]", r.status_code, r.text)

    try:
        return r.json(), r.status_code
    except:
        return None, r.status_code


def get_position(symbol):
    r = requests.get(POSITIONS_URL, headers=HEADERS)
    if r.status_code != 200:
        return None
    for p in r.json():
        if p["symbol"] == symbol:
            return p
    return None


async def ladder_buy(symbol, qty, entry):
    price = entry
    for step in range(6):
        body, code = place_limit_order(symbol, qty, "buy", price)
        print(f"[BUY STEP {step+1}] TRY @ {price}")

        await asyncio.sleep(5)

        pos = get_position(symbol)
        if pos:
            print(f"[BUY FILLED] {symbol} @ {pos['avg_entry_price']}")
            return float(pos["avg_entry_price"]), float(pos["qty"])

        price = round(price + 0.01, 2)

    print("[BUY FAILED] Skipping trade.")
    return None, None


async def ladder_exit(symbol, hard_stop, current):
    price = current
    for step in range(6):
        pos = get_position(symbol)
        if not pos:
            print("[EXIT COMPLETE]")
            return

        qty = float(pos["qty"])
        print(f"[EXIT STEP {step+1}] TRY SELL {qty} @ {price}")

        place_limit_order(symbol, qty, "sell", price)
        await asyncio.sleep(5)

        if not get_position(symbol):
            print("[EXIT FILLED]")
            return

        price = round(price - 0.01, 2)

    # aggressive final
    pos = get_position(symbol)
    if pos:
        qty = float(pos["qty"])
        print("[AGGRESSIVE EXIT] FINAL SELL")
        place_limit_order(symbol, qty, "sell", round(price - 0.03, 2))


async def monitor(symbol, stop, target, entry_price, qty):
    global stream

    highest = entry_price
    print(f"[MONITOR] {symbol} START — ENTRY {entry_price}")

    async def on_trade(t):
        nonlocal highest

        price = float(t.price)
        if price > highest:
            highest = price

        print(f"[PRICE] {symbol} = {price} (H:{highest}) STOP:{stop} TARGET:{target}")

        if price <= stop:
            print("[STOP HIT]")
            await ladder_exit(symbol, stop, price)
            await stream.stop()
        elif price >= target:
            print("[TARGET HIT]")
            await ladder_exit(symbol, stop, price)
            await stream.stop()

    stream = StockDataStream(API_KEY, SECRET_KEY, feed="sip")
    stream.subscribe_trades(on_trade, symbol)
    await stream.run()
    print("[MONITOR CLOSED]")

@app.route("/tv", methods=["POST"])
def webhook():
    global active_trade, stream_thread

    data = request.get_json(force=True)
    print("[WEBHOOK]", data)

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 403

    with trade_lock:
        if active_trade is not None:
            return jsonify({"error": "trade already running"}), 409

        symbol = data["ticker"].upper()
        qty = int(data["quantity"])
        entry = float(data["entry"])
        stop = float(data["stop"])
        target = float(data["target"])
        trail = float(data.get("trail", 15))

        async def start_trade():
            buy_price, filled_qty = await ladder_buy(symbol, qty, entry)
            if not buy_price:
                return

            active_trade = True
            await monitor(symbol, stop, target, buy_price, filled_qty)

            active_trade = None
            print("[CLEAN ACTIVE TRADE]")

        stream_thread = threading.Thread(
            target=lambda: asyncio.run(start_trade()),
            daemon=True
        )
        stream_thread.start()

        return jsonify({"msg": "trade started"}), 200

@app.route("/")
def health():
    return "Bot Running", 200

if __name__ == "__main__":
    app.run(port=8080)



























































































































