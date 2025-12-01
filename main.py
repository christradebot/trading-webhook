import os
import threading
import asyncio
from flask import Flask, request, jsonify
import requests
from alpaca.data.live import StockDataStream

# -----------------------------
# ENVIRONMENT
# -----------------------------
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


# ================================
# SAFE REQUEST WRAPPER
# ================================
def safe(method, url, **kwargs):
    try:
        r = requests.request(method, url, headers=HEADERS, timeout=5, **kwargs)
        return r
    except Exception as e:
        print(f"REQUEST ERROR: {e}")
        return None


# ================================
# ORDER HELPERS (blocking)
# ================================
def place_limit_order(symbol, qty, side, price):
    order = {
        "symbol": symbol,
        "qty": int(qty),
        "side": side,
        "type": "limit",
        "limit_price": f"{price:.2f}",
        "time_in_force": "day",
        "extended_hours": True
    }

    print(f"SEND ORDER: {order}")

    r = safe("POST", ORDERS_URL, json=order)
    if r is None:
        return None, 500

    print("ALPACA:", r.status_code, r.text)

    try:
        return r.json(), r.status_code
    except:
        return {"error": r.text}, r.status_code


def cancel_order(order_id):
    safe("DELETE", f"{ORDERS_URL}/{order_id}")


def get_position(symbol):
    r = safe("GET", POSITIONS_URL)
    if not r or r.status_code != 200:
        return None

    try:
        for p in r.json():
            if p["symbol"] == symbol:
                return p
    except:
        return None
    return None


def has_position(symbol):
    p = get_position(symbol)
    if not p:
        return False
    return float(p.get("qty", 0)) > 0


def get_order(order_id):
    r = safe("GET", f"{ORDERS_URL}/{order_id}")
    if r and r.status_code == 200:
        return r.json()
    return None


# ================================
# WAIT FOR FILL (async)
# ================================
async def wait_for_fill(symbol, order_ids):
    print("WAITING FOR BUY FILL...")

    while True:
        # Check all ladder orders
        for oid in order_ids:
            o = await asyncio.to_thread(get_order, oid)
            if o and o.get("status") in ["canceled", "rejected", "expired"]:
                continue

        pos = await asyncio.to_thread(get_position, symbol)
        if pos and float(pos["qty"]) > 0:
            price = float(pos["avg_entry_price"])
            qty = float(pos["qty"])
            print(f"BUY FILLED @ {price} | QTY {qty}")
            return price, qty

        await asyncio.sleep(1)


# ================================
# LADDER BUY (async)
# ================================
async def ladder_buy(symbol, qty, entry_price):
    print("LADDER BUY STARTING")

    order_ids = []
    price = entry_price

    for step in range(6):
        o, s = await asyncio.to_thread(
            place_limit_order, symbol, qty, "buy", price
        )
        if s in [200, 201] and "id" in o:
            order_ids.append(o["id"])
        await asyncio.sleep(5)
        price += 0.01  # upward ladder

        # If filled already → stop laddering
        if await asyncio.to_thread(has_position, symbol):
            break

    # Cancel any remaining unfilled orders
    for oid in order_ids:
        await asyncio.to_thread(cancel_order, oid)

    return await wait_for_fill(symbol, order_ids)


# ================================
# LADDER EXIT (async)
# ================================
async def ladder_exit(symbol, current_price, hard_stop):
    print("EXIT SEQUENCE STARTED")

    price = current_price

    for step in range(6):
        pos = await asyncio.to_thread(get_position, symbol)
        if not pos or float(pos["qty"]) <= 0:
            print("EXIT COMPLETE")
            return

        qty = float(pos["qty"])

        if price < hard_stop:
            price = hard_stop

        await asyncio.to_thread(place_limit_order, symbol, qty, "sell", price)

        await asyncio.sleep(5)

        if not await asyncio.to_thread(has_position, symbol):
            print("EXIT COMPLETE")
            return

        price -= 0.01

    # FINAL AGGRESSIVE EXIT
    pos = await asyncio.to_thread(get_position, symbol)
    if pos:
        qty = float(pos["qty"])
        await asyncio.to_thread(place_limit_order, symbol, qty, "sell", price)


# ================================
# WEBSOCKET MONITOR
# ================================
def websocket_thread(trade):
    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()

    async def run():
        print(f"MONITORING {trade['symbol']}")

        stream = StockDataStream(API_KEY, SECRET_KEY, feed="sip")

        filled_entry = trade["filled_entry"]
        qty = trade["filled_qty"]
        highest = filled_entry
        trail_active = False

        async def handler(msg):
            nonlocal highest, trail_active

            price = float(msg.price)
            symbol = trade["symbol"]

            if price > highest:
                highest = price

            stop = trade["stop"]
            target = trade["target"]

            if price >= filled_entry * 1.2:
                trail_active = True

            if trail_active:
                trail_stop = round(highest * (1 - trade["trail"]), 2)
                stop = max(stop, trail_stop)

            # STOP / TARGET
            if price <= stop or price >= target:
                print(f"EXIT TRIGGER @ {price}")
                await ladder_exit(symbol, price, trade["stop"])

                pos = await asyncio.to_thread(get_position, symbol)
                if not pos:
                    pnl = round((price - filled_entry) * qty, 2)
                    pct = round((price / filled_entry - 1) * 100, 2)
                    print(f"P/L: ${pnl} ({pct}%)")
                await stream.stop()

        try:
            stream.subscribe_trades(handler, trade["symbol"])
            await stream.run()
        finally:
            global active_trade
            with trade_lock:
                active_trade = None

    loop.run_until_complete(run())


# ================================
# FLASK ENDPOINTS
# ================================
@app.route("/")
def health():
    return "Bot OK", 200


@app.route("/tv", methods=["POST"])
def tv():
    global active_trade

    data = request.get_json(force=True)
    print("SIGNAL RECEIVED:", data)

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 403

    symbol = data["ticker"]
    qty = int(data["quantity"])
    entry = float(data["entry"])
    stop = float(data["stop"])
    target = float(data["target"])
    trail = float(data.get("trail", 0.15))

    with trade_lock:
        if active_trade:
            return jsonify({"error": "trade already active"}), 409

        filled_price, filled_qty = asyncio.new_event_loop().run_until_complete(
            ladder_buy(symbol, qty, entry)
        )

        active_trade = {
            "symbol": symbol,
            "filled_entry": filled_price,
            "filled_qty": filled_qty,
            "stop": stop,
            "target": target,
            "trail": trail,
        }

        t = threading.Thread(target=websocket_thread, args=(active_trade,), daemon=True)
        t.start()

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)



























































































































