import os, time, threading
from flask import Flask, request, jsonify
from alpaca_trade_api import REST

API_KEY = os.getenv("ALPACA_API_KEY")
API_SECRET = os.getenv("ALPACA_API_SECRET")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

api = REST(API_KEY, API_SECRET, BASE_URL)

app = Flask(__name__)

ACTIVE_PLAN = None
SECURITY_KEY = "CHRISBOT1501"


# ========================
# PRICE FETCH (LAST TRADE)
# ========================
def get_last_price(symbol):
    trade = api.get_latest_trade(symbol)
    return float(trade.price) if trade else None


# ========================
# LADDER ENGINE
# ========================
def ladder_order(symbol, side, base_price, qty, upward=True):
    steps = [0, .01, .02, .03, .04, .05]

    for i, step in enumerate(steps):
        if upward:
            price = round(base_price + step, 4)
        else:
            price = round(base_price - step, 4)

        print(f"[LADDER] {side} try {i+1}/6 at {price}")

        try:
            api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type='limit',
                limit_price=str(price),
                time_in_force='gtc'
            )
        except Exception as e:
            print(f"[ERROR] Order submit failed: {e}")

        time.sleep(5)

        pos = get_position(symbol)
        if pos:
            print(f"[FILLED] {symbol} at approx {pos['price']}")
            return True

    return False


# ========================
# POSITION CHECK
# ========================
def get_position(symbol):
    try:
        pos = api.get_position(symbol)
        return {"qty": float(pos.qty), "price": float(pos.avg_entry_price)}
    except:
        return None


# ========================
# STOP LOSS ENGINE
# ========================
def run_stop_loss(plan):
    symbol = plan["ticker"]
    stop = plan["stop"]
    qty = plan["quantity"]

    success = ladder_order(symbol, "sell", stop, qty, upward=False)

    if not success:
        # FINAL AGGRESSIVE EXIT
        price = get_last_price(symbol)
        emergency = round(price * 0.98, 4)

        print(f"[EMERGENCY EXIT] at {emergency}")

        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="limit",
            limit_price=str(emergency),
            time_in_force="gtc"
        )


# ========================
# TARGET ENGINE
# ========================
def run_target(plan):
    symbol = plan["ticker"]
    qty = plan["quantity"]
    target = plan["target"]

    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell',
            type='limit',
            limit_price=str(target),
            time_in_force='gtc'
        )
        print(f"[TARGET] Order placed at {target}")
    except Exception as e:
        print(f"[ERROR] Target order: {e}")


# ========================
# MONITOR THREAD
# ========================
def monitor_trade():
    global ACTIVE_PLAN

    plan = ACTIVE_PLAN
    symbol = plan["ticker"]
    qty = plan["quantity"]
    entry = plan["entry"]
    trail_pct = plan["trail_pct"]

    print("[MONITOR] Starting for", symbol)

    # ENTRY
    success = ladder_order(symbol, "buy", entry, qty, upward=True)
    if not success:
        print("[FAILED] Entry not filled in 30 sec")
        ACTIVE_PLAN = None
        return

    # TARGET
    run_target(plan)

    highest = entry

    while True:
        time.sleep(1)

        price = get_last_price(symbol)
        pos = get_position(symbol)

        if not pos:
            print("[EXITED]")
            break

        if price > highest:
            highest = price

        trail_trigger = highest * (1 - (trail_pct / 100))

        if price <= trail_trigger:
            print("[TRAIL HIT]")
            run_stop_loss(plan)
            break


# ========================
# WEBHOOK
# ========================
@app.route("/webhook", methods=["POST"])
def webhook():
    global ACTIVE_PLAN

    data = request.json

    if data.get("secret") != SECURITY_KEY:
        return jsonify({"error": "invalid secret"}), 403

    ACTIVE_PLAN = {
        "ticker": data["ticker"],
        "quantity": int(data["quantity"]),
        "entry": float(data["entry"]),
        "stop": float(data["stop"]),
        "target": float(data["target"]),
        "trail_pct": float(data["trail_pct"])
    }

    threading.Thread(target=monitor_trade).start()

    return jsonify({"status": "PLAN LOADED"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)










































































































