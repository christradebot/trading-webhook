import os
import json
import time
import traceback
from datetime import datetime
from flask import Flask, request, jsonify

# Trading & Data Clients
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

LIVE_MODE = True                 # live trading
MAX_SPREAD_PCT = float(os.environ.get("MAX_SPREAD_PCT", "5"))

# Trading window — UTC
TRADE_START_UTC_HOUR = int(os.environ.get("TRADE_START_UTC_HOUR", "4"))
TRADE_END_UTC_HOUR   = int(os.environ.get("TRADE_END_UTC_HOUR", "20"))

MAX_ORDER_ATTEMPTS = 3

if not all([API_KEY, SECRET_KEY, BASE_URL]):
    print("[FATAL] Missing Alpaca API credentials.")
    exit(1)

# Trading API
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=not LIVE_MODE)

# Data API
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

app = Flask(__name__)

# =====================================================================
# GLOBAL PLAN
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
        log(f"[TIME GUARD] Outside allowed UTC window (hour={h})")
    return allowed

# =====================================================================
# POSITION CHECK
# =====================================================================

def has_open_position(symbol):
    try:
        pos = trading_client.get_open_position(symbol)
        qty = float(pos.qty)
        log(f"[POSITION] Have open position qty={qty}")
        return qty > 0
    except:
        log("[POSITION] No open position.")
        return False

# =====================================================================
# PAYLOAD PARSER
# =====================================================================

def parse_webhook_payload(req):
    try:
        raw = req.data.decode("utf-8").strip()
        log(f"RAW BODY: {raw}")
        payload = json.loads(raw)
        log(f"PARSED: {payload}")
        return payload
    except:
        log("[ERROR] JSON decode failed")
        traceback.print_exc()
        return None

# =====================================================================
# SEND LIMIT ORDER
# =====================================================================

def submit_limit_order(symbol, qty, price, side):
    try:
        price = round(float(price), 4)
    except:
        log("[ORDER ERROR] Bad price")
        return None

    if price <= 0 or qty <= 0:
        log("[ORDER ERROR] Invalid qty/price")
        return None

    limit_price = round(price + 0.01, 4) if side == OrderSide.BUY else round(price - 0.01, 4)

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
            log(f"[ORDER PLACED] Attempt={attempt}: {order}")
            return order

        except Exception as e:
            log(f"[ORDER ERROR] Attempt={attempt} → {e}")
            time.sleep(1)

    log("[ORDER FAILURE] All attempts failed.")
    return None

# =====================================================================
# QUOTES (CORRECT DATA API VERSION)
# =====================================================================

def get_latest_bid_ask(symbol):
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quote = data_client.get_stock_latest_quote(req)

        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)

        if bid <= 0 or ask <= 0:
            log(f"[QUOTE] Invalid bid/ask: bid={bid} ask={ask}")
            return None, None, None

        spread = ask - bid
        spread_pct = (spread / ask) * 100

        log(f"[QUOTE] {symbol} bid={bid}, ask={ask}, spread={spread_pct:.2f}%")
        return bid, ask, spread_pct

    except Exception as e:
        log(f"[ERROR] Quote failure: {e}")
        return None, None, None

# =====================================================================
# MAIN EXECUTION LOOP
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

            # =============================
            # ENTRY
            # =============================
            if not active_plan["entry_filled"]:
                if has_open_position(symbol):
                    log("[ENTRY] Already in position — skipping buy.")
                    active_plan["entry_filled"] = True
                    active_plan["in_position"] = True
                else:
                    if spread_pct > MAX_SPREAD_PCT:
                        log(f"[SPREAD GUARD] spread {spread_pct:.2f}% > max {MAX_SPREAD_PCT}%")
                    else:
                        if bid >= active_plan["entry"]:
                            log("[ENTRY] BUY TRIGGERED")

                            order = submit_limit_order(
                                symbol,
                                active_plan["qty"],
                                active_plan["entry"],
                                OrderSide.BUY
                            )

                            if order:
                                active_plan["entry_filled"] = True
                                active_plan["in_position"] = True
                                active_plan["highest_bid"] = bid
                                active_plan["trail_active"] = active_plan["trail_pct"] > 0
                                log("[ENTRY] Buy filled.")

            # No position → skip exit logic
            if not active_plan["in_position"]:
                time.sleep(1)
                continue

            # Position disappeared?
            if not has_open_position(symbol):
                log("[SYNC] Alpaca shows flat — resetting.")
                active_plan["in_position"] = False
                time.sleep(1)
                continue

            # =============================
            # TARGET
            # =============================
            if not active_plan["target_sent"] and bid >= active_plan["target"]:
                log("[TARGET] SELL TRIGGERED")

                if has_open_position(symbol):
                    order = submit_limit_order(
                        symbol,
                        active_plan["qty"],
                        active_plan["target"],
                        OrderSide.SELL
                    )
                    if order:
                        active_plan["target_sent"] = True
                        active_plan["in_position"] = False
                        active_plan["trail_active"] = False
                        log("[TARGET] Closed on target.")
                time.sleep(1)
                continue

            # =============================
            # STOP LOSS
            # =============================
            if not active_plan["stop_sent"] and bid <= active_plan["stop"]:
                log("[STOP] SELL TRIGGERED")

                if has_open_position(symbol):
                    order = submit_limit_order(
                        symbol,
                        active_plan["qty"],
                        active_plan["stop"],
                        OrderSide.SELL
                    )
                    if order:
                        active_plan["stop_sent"] = True
                        active_plan["in_position"] = False
                        active_plan["trail_active"] = False
                        log("[STOP] Closed on stop.")
                time.sleep(1)
                continue

            # =============================
            # TRAILING STOP
            # =============================
            if (
                active_plan["trail_active"]
                and not active_plan["trail_sent"]
                and active_plan["entry_filled"]
            ):
                highest = active_plan["highest_bid"]

                if highest is None or bid > highest:
                    active_plan["highest_bid"] = bid
                    highest = bid

                trail_pct = active_plan["trail_pct"]
                trail_level = highest * (1 - trail_pct / 100)

                log(f"[TRAIL] highest={highest:.4f}, trail_level={trail_level:.4f}, bid={bid:.4f}")

                if bid <= trail_level:
                    log("[TRAIL] SELL TRIGGERED")

                    if has_open_position(symbol):
                        order = submit_limit_order(
                            symbol,
                            active_plan["qty"],
                            bid,
                            OrderSide.SELL
                        )
                        if order:
                            active_plan["trail_sent"] = True
                            active_plan["in_position"] = False
                            active_plan["trail_active"] = False
                            log("[TRAIL] Closed by trailing stop.")

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
        return jsonify({"status": "error", "message": "invalid_json"}), 400

    # Secret
    if str(payload.get("secret")) != str(WEBHOOK_SECRET):
        log("[ERROR] BAD SECRET")
        return jsonify({"status": "error", "message": "bad_secret"}), 401

    log("[SECRET] VALID")

    # Extract fields
    try:
        ticker = payload["ticker"].upper()
        qty = int(payload["quantity"])
        entry = float(payload["entry"])
        stop = float(payload["stop"])
        target = float(payload["target"])
        trail_pct = float(payload.get("trail_pct", 0))

    except Exception as e:
        log(f"[PAYLOAD ERROR] {e}")
        return jsonify({"status": "error", "message": "bad_payload"}), 400

    # Store plan
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
# RUN SERVER
# =====================================================================

if __name__ == "__main__":
    log(f"SERVER STARTED — live={LIVE_MODE}, UTC_window={TRADE_START_UTC_HOUR}-{TRADE_END_UTC_HOUR}, max_spread={MAX_SPREAD_PCT}%")
    app.run(host="0.0.0.0", port=8080)



































































































