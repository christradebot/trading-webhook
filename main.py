import os
import threading
import asyncio
from flask import Flask, request, jsonify
import requests

# Optional .env support (safe if not present)
try:
    from dotenv import load_dotenv
    load_dotenv()
except:
    pass

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

if not API_KEY or not SECRET_KEY or not WEBHOOK_SECRET:
    print("❌ CRITICAL ERROR: Missing environment variables")

trade_lock = threading.Lock()
active_trade = None  # Only 1 active trade allowed

# ===================== HELPERS (blocking) ======================

def safe_request(method, url, **kwargs):
    try:
        r = requests.request(method, url, headers=HEADERS, timeout=5, **kwargs)
        return r
    except Exception as e:
        print(f"REQUEST ERROR: {e}")
        return None


def place_limit_order(symbol, qty, side, price):

    qty = int(qty)
    if qty <= 0:
        print("❌ BAD QTY")
        return None, 400

    order = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "limit",
        "limit_price": str(round(float(price), 4)),
        "time_in_force": "day",
        "extended_hours": True
    }

    print("📤 ORDER:", order)

    r = safe_request("POST", ORDERS_URL, json=order)

    if not r:
        return None, 500

    print("📥 ALPACA:", r.status_code, r.text)

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

    if not pos:
        return False

    try:
        return float(pos["qty"]) > 0
    except:
        return False


def get_order_status(order_id):

    r = safe_request("GET", f"{ORDERS_URL}/{order_id}")

    if r and r.status_code == 200:
        try:
            return r.json()
        except:
            return None

    return None


# ===================== WAIT FOR FILL ======================

async def wait_for_fill(symbol, order_id):

    print(f"⏳ WAITING FOR FILL {symbol}")

    while True:

        status = await asyncio.to_thread(get_order_status, order_id)

        if status:
            if status.get("status") in ["canceled", "rejected", "expired"]:
                print(f"❌ ORDER {status['status'].upper()}")
                return None, None

        pos = await asyncio.to_thread(get_position, symbol)

        if pos and float(pos["qty"]) > 0:
            entry_price = float(pos.get("avg_entry_price", 0))
            qty = float(pos["qty"])
            print(f"✅ FILLED {symbol} @ {entry_price}")
            return entry_price, qty

        await asyncio.sleep(2)


# ====================== LADDER EXIT ======================

async def ladder_exit(symbol, start_price, hard_stop):

    print("🪜 LADDER EXIT STARTED")
    price = float(start_price)

    for i in range(6):

        pos = await asyncio.to_thread(get_position, symbol)

        if not pos or float(pos["qty"]) <= 0:
            print("✅ CLOSED")
            return

        qty = float(pos["qty"])

        if price < hard_stop:
            price = hard_stop

        print(f"ATTEMPT {i+1} @ {price} QTY {qty}")

        await asyncio.to_thread(
            place_limit_order, symbol, qty, "sell", price
        )

        await asyncio.sleep(5)

        if not await asyncio.to_thread(has_position, symbol):
            print("✅ CLOSED")
            return

        price = round(price - 0.01, 4)

    final_pos = await asyncio.to_thread(get_position, symbol)

    if final_pos and float(final_pos["qty"]) > 0:
        print("⚠️ FINAL EXIT")
        await asyncio.to_thread(
            place_limit_order, symbol, float(final_pos["qty"]), "sell", price
        )


# ====================== WEBSOCKET ======================

def start_websocket(trade):

    global active_trade

    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()

    async def runner():

        try:

            print(f"📡 MONITORING {trade['symbol']}")

            stream = StockDataStream(API_KEY, SECRET_KEY, feed="sip")

            entry_price, qty = await wait_for_fill(
                trade["symbol"],
                trade["order_id"]
            )

            if entry_price is None:
                print("❌ NEVER FILLED – STOPPING")
                return

            highest = entry_price
            trail_active = False

            async def on_trade(data):

                nonlocal highest, trail_active

                price = float(data.price)
                symbol = trade["symbol"]

                # ✅ ULTRASAFE LONG PROTECTION
                pos_exist = await asyncio.to_thread(has_position, symbol)
                if not pos_exist:
                    print("🔒 ULTRASAFE: No long position – blocking sell.")
                    return

                if price > highest:
                    highest = price

                if not trail_active and price >= entry_price * 1.20:
                    trail_active = True
                    print("🔥 TRAILING ACTIVATED")

                stop = trade["stop"]
                target = trade["target"]

                if trail_active:
                    trail_stop = round(highest * (1 - trade["trail"] / 100), 4)
                    stop = max(stop, trail_stop)

                print(
                    f"{symbol} | PRICE {price} | HIGH {highest} | STOP {stop} | TARGET {target}"
                )

                if price <= stop or price >= target:
                    print("🚨 EXIT HIT")
                    await ladder_exit(symbol, price, trade["stop"])
                    await stream.stop()

            stream.subscribe_trades(on_trade, trade["symbol"])
            await stream.run()

        except Exception as e:
            print(f"CRITICAL ERROR: {e}")

        finally:
            with trade_lock:
                active_trade = None
                print("🧹 CLEANED ACTIVE TRADE")

    loop.run_until_complete(runner())


# ====================== FLASK ======================

@app.route("/")
def health():
    return "Bot Online ✅", 200


@app.route("/tv", methods=["POST"])
def webhook():

    global active_trade

    try:
        data = request.get_json(force=True)
        print("WEBHOOK:", data)
    except:
        return jsonify({"error": "Bad JSON"}), 400

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    try:
        symbol = data["ticker"]
        qty = int(data["quantity"])
        entry = float(data["entry"])
        stop = float(data["stop"])
        target = float(data["target"])
        trail = float(data.get("trail", 15))
    except Exception as e:
        return jsonify({"error": "Bad payload", "detail": str(e)}), 400

    with trade_lock:

        if active_trade is not None:
            return jsonify({"error": f"Already active on {active_trade['symbol']}"}), 429

        order_json, status = place_limit_order(symbol, qty, "buy", entry)

        if status not in [200, 201] or "id" not in order_json:
            return jsonify({"error": "Buy failed", "alpaca": order_json}), 500

        order_id = order_json["id"]

        active_trade = {
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "stop": stop,
            "target": target,
            "trail": trail,
            "order_id": order_id
        }

        t = threading.Thread(
            target=start_websocket,
            args=(active_trade,),
            daemon=True
        )
        t.start()

    return jsonify({"msg": f"{symbol} LIVE — Order {order_id}"}), 200


if __name__ == "__main__":

    print("🚀 BOT STARTING — SIP + ULTRASAFE")
    app.run(host="0.0.0.0", port=8080)























































































































