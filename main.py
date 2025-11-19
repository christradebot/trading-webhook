import os
import json
import time
import traceback
from datetime import datetime
from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# =====================================================================
# CONFIG
# =====================================================================

API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
BASE_URL = os.environ.get("APCA_API_BASE_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

# 🔒 TEST MODE (NO ORDERS SENT)
TEST_MODE = True

if not all([API_KEY, SECRET_KEY, BASE_URL]):
    print("[FATAL] Missing Alpaca API credentials.")
    exit(1)

# Still initialize client (safe in TEST_MODE)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=False)

app = Flask(__name__)

# GLOBAL STATE (1 active plan only)
active_plan = {
    "ticker": None,
    "qty": 0,
    "entry": None,
    "stop": None,
    "target": None,
    "trail_pct": None,
    "in_position": False,
    "entry_filled": False,
    "stop_sent": False,
    "target_sent": False
}

# =====================================================================
# LOGGING
# =====================================================================

def log(msg):
    print(f"[{datetime.utcnow()}] {msg}", flush=True)

# =====================================================================
# PARSE WEBHOOK JSON
# =====================================================================

def parse_webhook_payload(req):
    try:
        raw = req.data.decode("utf-8").strip()
        log(f"RAW Incoming Body: {raw}")

        payload = json.loads(raw)
        log(f"Parsed Payload: {payload}")
        return payload

    except Exception as e:
        log(f"[ERROR] Failed JSON decode: {e}")
        traceback.print_exc()
        return None

# =====================================================================
# SEND LIMIT ORDER (SAFE TEST MODE)
# =====================================================================

def submit_limit_order(symbol, qty, price, side):
    """
    In TEST_MODE this function does NOT submit any order.
    """

    price = round(float(price), 2)

    if side == OrderSide.BUY:
        limit_price = round(price + 0.01, 2)
    else:
        limit_price = round(price - 0.01, 2)

    log(f"[TEST_MODE={TEST_MODE}] Prepared {side} LIMIT order for {symbol} at {limit_price}")

    if TEST_MODE:
        log("⚠️ TEST MODE ENABLED — ORDER NOT SENT")
        return {
            "test_mode": True,
            "symbol": symbol,
            "qty": qty,
            "side": str(side),
            "limit_price": limit_price
        }

    try:
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=limit_price,
            time_in_force=TimeInForce.DAY
        )
        order = trading_client.submit_order(req)
        log(f"ORDER SENT: {order}")
        return order

    except Exception as e:
        log(f"[ORDER ERROR] {e}")
        traceback.print_exc()
        return None

# =====================================================================
# PRICE CHECKER (STILL RUNS IN TEST MODE)
# =====================================================================

def get_current_bid(symbol):
    try:
        quote = trading_client.get_latest_quote(symbol)
        bid = float(quote.bid_price)
        log(f"Latest bid for {symbol} = {bid}")
        return bid
    except:
        log("[ERROR] Could not get quote.")
        return None

# =====================================================================
# ORDER EXECUTION LOGIC
# =====================================================================

def monitor_price():
    while True:
        if active_plan["ticker"] is None:
            time.sleep(1)
            continue

        symbol = active_plan["ticker"]
        bid = get_current_bid(symbol)

        if bid is None:
            time.sleep(1)
            continue

        # ENTRY
        if not active_plan["entry_filled"]:
            if bid >= active_plan["entry"]:
                log("ENTRY TRIGGERED → BUY (TEST MODE)")

                submit_limit_order(
                    symbol=symbol,
                    qty=active_plan["qty"],
                    price=active_plan["entry"],
                    side=OrderSide.BUY
                )

                active_plan["entry_filled"] = True
                active_plan["in_position"] = True

        # STOP
        if active_plan["entry_filled"] and not active_plan["stop_sent"]:
            if bid <= active_plan["stop"]:
                log("STOP HIT → SELL (TEST MODE)")

                submit_limit_order(
                    symbol=symbol,
                    qty=active_plan["qty"],
                    price=active_plan["stop"],
                    side=OrderSide.SELL
                )

                active_plan["stop_sent"] = True
                active_plan["in_position"] = False

        # TARGET
        if active_plan["entry_filled"] and not active_plan["target_sent"]:
            if bid >= active_plan["target"]:
                log("TARGET HIT → SELL (TEST MODE)")

                submit_limit_order(
                    symbol=symbol,
                    qty=active_plan["qty"],
                    price=active_plan["target"],
                    side=OrderSide.SELL
                )

                active_plan["target_sent"] = True
                active_plan["in_position"] = False

        time.sleep(1)

# =====================================================================
# WEBHOOK
# =====================================================================

@app.route("/tv", methods=["POST"])
def tv_webhook():
    payload = parse_webhook_payload(request)

    if payload is None:
        return jsonify({"status": "error", "message": "invalid_json"}), 400

    # Secret check
    if str(payload.get("secret")) != str(WEBHOOK_SECRET):
        log("[ERROR] SECRET INVALID")
        return jsonify({"status": "error", "message": "bad_secret"}), 401

    log("SECRET VALID")

    try:
        ticker = payload["ticker"]
        qty = int(payload["quantity"])
        entry = float(payload["entry"])
        stop = float(payload["stop"])
        target = float(payload["target"])
        trail_pct = float(payload.get("trail_pct", 15))

    except Exception as e:
        log(f"[PAYLOAD ERROR] {e}")
        return jsonify({"status": "error", "message": "bad_payload"}), 400

    active_plan.update({
        "ticker": ticker,
        "qty": qty,
        "entry": entry,
        "stop": stop,
        "target": target,
        "trail_pct": trail_pct,
        "in_position": False,
        "entry_filled": False,
        "stop_sent": False,
        "target_sent": False
    })

    log(f"PLAN STORED: {active_plan}")

    return jsonify({"status": "ok", "message": "plan_loaded"}), 200

# =====================================================================
# START MONITOR THREAD
# =====================================================================

import threading
threading.Thread(target=monitor_price, daemon=True).start()

# =====================================================================
# RUN FLASK
# =====================================================================

if __name__ == "__main__":
    log("TEST MODE SERVER RUNNING — NO ORDERS WILL BE SENT")
    app.run(host="0.0.0.0", port=8080)































































































