import os
import json
import time
import traceback
from datetime import datetime
from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# =====================================================================
# CONFIG
# =====================================================================

API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
BASE_URL = os.environ.get("APCA_API_BASE_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

LIVE_MODE = True

MAX_SPREAD_PCT = float(os.environ.get("MAX_SPREAD_PCT", "5"))
TRADE_START_UTC_HOUR = int(os.environ.get("TRADE_START_UTC_HOUR", "4"))
TRADE_END_UTC_HOUR   = int(os.environ.get("TRADE_END_UTC_HOUR", "20"))
MAX_ORDER_ATTEMPTS = 3

if not all([API_KEY, SECRET_KEY, BASE_URL]):
    print("[FATAL] Missing Alpaca API credentials.")
    exit(1)

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=not LIVE_MODE)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

app = Flask(__name__)

# =====================================================================
# GLOBAL STATE
# =====================================================================

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
    "target_sent": False,
    "trail_sent": False,
    "trail_active": False,
    "highest_bid": None
}

# =====================================================================
# LOGGING
# =====================================================================

def log(msg):
    print(f"[{datetime.utcnow()}] {msg}", flush=True)

# =====================================================================
# TIME GUARD
# =====================================================================

def is_trading_time():
    now = datetime.utcnow()
    h = now.hour
    allowed = TRADE_START_UTC_HOUR <= h < TRADE_END_UTC_HOUR
    if not allowed:
        log(f"[TIME GUARD] Outside allowed UTC hours {TRADE_START_UTC_HOUR}-{TRADE_END_UTC_HOUR}")
    return allowed

# =====================================================================
# POSITION CHECK
# =====================================================================

def has_open_position(symbol):
    try:
        pos = trading_client.get_open_position(symbol)
        qty = float(pos.qty)
        log(f"[POSITION] Open position for {symbol}: qty={qty}")
        return qty != 0
    except:
        log(f"[POSITION] No open position for {symbol}")
        return False

# =====================================================================
# WEBHOOK PARSER
# =====================================================================

def parse_webhook_payload(req):
    try:
        raw = req.data.decode("utf-8").strip()
        log(f"RAW BODY: {raw}")
        payload = json.loads(raw)
        log(f"PARSED: {payload}")
        return payload
    except:
        log("[ERROR] JSON decode failed.")
        traceback.print_exc()
        return None

# =====================================================================
# LIMIT ORDER
# =====================================================================

def submit_limit_order(symbol, qty, price, side):

    try:
        price = round(float(price), 4)
    except:
        log(f"[ORDER ERROR] Invalid price: {price}")
        return None

    if price <= 0 or qty <= 0:
        log("[ORDER ERROR] price/qty <= 0, refusing order")
        return None

    if side == OrderSide.BUY:
        limit_price = round(price + 0.01, 4)
    else:
        limit_price = round(price - 0.01, 4)

    for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
        try:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                limit_price=limit_price,
                time_in_force=TimeInForce.DAY
            )
            order = trading_client.submit_order(req)
            log(f"[ORDER SUCCESS] {order}")
            return order
        except Exception as e:
            log(f"[ORDER ERROR] attempt {attempt} failed → {e}")
            traceback.print_exc()
            time.sleep(1)

    log("[ORDER ERROR] All attempts failed.")
    return None

# =====================================================================
# FIXED QUOTE READER (FINAL VERSION)
# =====================================================================

def get_latest_bid_ask(symbol):
    """
    Alpaca returns:
        { "STEC": Quote(bp=..., ap=..., ...) }

    Quote objects do NOT support `.get()`, so we must read attributes.
    """
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        resp = data_client.get_stock_latest_quote(req)

        # resp is ALWAYS a dict when passing list/str
        if isinstance(resp, dict):
            quote = list(resp.values())[0]
        else:
            quote = resp

        # Preferred attributes: bp (bid price) and ap (ask price)
        bid = float(getattr(quote, "bp", 0))
        ask = float(getattr(quote, "ap", 0))

        # fallback for older formats
        if bid <= 0:
            bid = float(getattr(quote, "bid_price", bid))
        if ask <= 0:
            ask = float(getattr(quote, "ask_price", ask))

        if bid <= 0 or ask <= 0:
            log(f"[QUOTE] Invalid bid/ask for {symbol}: bid={bid}, ask={ask}")
            return None, None, None

        spread_pct = (ask - bid) / ask * 100
        log(f"[QUOTE] {symbol} bid={bid}, ask={ask}, spread={spread_pct:.2f}%")

        return bid, ask, spread_pct

    except Exception as e:
        log(f"[ERROR] Quote failure: {e}")
        traceback.print_exc()
        return None, None, None

# =====================================================================
# MONITOR LOOP (UNCHANGED LOGIC)
# =====================================================================

def monitor_price():
    while True:
        try:
            if active_plan["ticker"] is None:
                time.sleep(1)
                continue

            symbol = active_plan["ticker"]

            if not is_trading_time():
                time.sleep(5)
                continue

            bid, ask, spread_pct = get_latest_bid_ask(symbol)
            if bid is None:
                time.sleep(1)
                continue

            # ENTRY LOGIC
            if not active_plan["entry_filled"]:

                if has_open_position(symbol):
                    active_plan["entry_filled"] = True
                    active_plan["in_position"] = True
                else:
                    if spread_pct > MAX_SPREAD_PCT:
                        log(f"[SPREAD GUARD] Spread too high: {spread_pct:.2f}%")
                    else:
                        if bid >= active_plan["entry"]:
                            order = submit_limit_order(symbol, active_plan["qty"], active_plan["entry"], OrderSide.BUY)
                            if order:
                                active_plan["entry_filled"] = True
                                active_plan["in_position"] = True
                                active_plan["highest_bid"] = bid
                                active_plan["trail_active"] = active_plan["trail_pct"] > 0

            if not active_plan["in_position"]:
                time.sleep(1)
                continue

            # HARD STOP
            if not active_plan["stop_sent"] and bid <= active_plan["stop"]:
                if has_open_position(symbol):
                    submit_limit_order(symbol, active_plan["qty"], active_plan["stop"], OrderSide.SELL)
                active_plan["stop_sent"] = True
                active_plan["in_position"] = False
                continue

            # TARGET
            if not active_plan["target_sent"] and bid >= active_plan["target"]:
                if has_open_position(symbol):
                    submit_limit_order(symbol, active_plan["qty"], active_plan["target"], OrderSide.SELL)
                active_plan["target_sent"] = True
                active_plan["in_position"] = False
                continue

            # TRAILING STOP
            if active_plan["trail_active"] and not active_plan["trail_sent"]:
                if bid > active_plan["highest_bid"]:
                    active_plan["highest_bid"] = bid

                trail_level = active_plan["highest_bid"] * (1 - active_plan["trail_pct"] / 100)

                if bid <= trail_level:
                    if has_open_position(symbol):
                        submit_limit_order(symbol, active_plan["qty"], bid, OrderSide.SELL)
                    active_plan["trail_sent"] = True
                    active_plan["in_position"] = False

            time.sleep(1)

        except Exception as e:
            log(f"[MONITOR ERROR] {e}")
            traceback.print_exc()
            time.sleep(2)

# =====================================================================
# WEBHOOK ENDPOINT
# =====================================================================

@app.route("/tv", methods=["POST"])
def tv_webhook():
    payload = parse_webhook_payload(request)
    if payload is None:
        return jsonify({"status": "error", "message": "bad_json"}), 400

    if str(payload.get("secret")) != str(WEBHOOK_SECRET):
        return jsonify({"status": "error", "message": "bad_secret"}), 401

    try:
        ticker = str(payload["ticker"]).upper()
        qty = int(payload["quantity"])
        entry = float(payload["entry"])
        stop = float(payload["stop"])
        target = float(payload["target"])
        trail_pct = float(payload.get("trail_pct", 0))
    except Exception:
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
        "target_sent": False,
        "trail_sent": False,
        "trail_active": False,
        "highest_bid": None
    })

    log(f"[PLAN LOADED] {active_plan}")

    return jsonify({"status": "ok", "message": "plan_loaded"}), 200

# =====================================================================
# START MONITOR THREAD
# =====================================================================

import threading
threading.Thread(target=monitor_price, daemon=True).start()

# =====================================================================
# FLASK RUN
# =====================================================================

if __name__ == "__main__":
    log(f"SERVER STARTED — LIVE_MODE={LIVE_MODE}, TRADE HOURS {TRADE_START_UTC_HOUR}-{TRADE_END_UTC_HOUR}, MAX_SPREAD={MAX_SPREAD_PCT}%")
    app.run(host="0.0.0.0", port=8080)





































































































