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
BASE_URL = os.environ.get("APCA_API_BASE_URL")  # not strictly needed but kept for clarity
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

# Live trading (paper=False). Change to False if you want to test on paper.
LIVE_MODE = True

# Spread safety (percentage, e.g. 5 = 5%)
MAX_SPREAD_PCT = float(os.environ.get("MAX_SPREAD_PCT", "5"))

# Allowed trading hours in UTC (24h format)
TRADE_START_UTC_HOUR = int(os.environ.get("TRADE_START_UTC_HOUR", "4"))   # 04:00 UTC
TRADE_END_UTC_HOUR   = int(os.environ.get("TRADE_END_UTC_HOUR", "20"))    # 20:00 UTC

# Order retry attempts
MAX_ORDER_ATTEMPTS = 3

if not all([API_KEY, SECRET_KEY]):
    print("[FATAL] Missing Alpaca API credentials.")
    exit(1)

# Trading client (orders)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=not LIVE_MODE)

# Data client (quotes)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

app = Flask(__name__)

# =====================================================================
# GLOBAL STATE (1 active plan only)
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
# UTILITY LOGGING
# =====================================================================

def log(msg):
    print(f"[{datetime.utcnow()}] {msg}", flush=True)

# =====================================================================
# TIME WINDOW CHECK
# =====================================================================

def is_trading_time():
    """
    Only allow trading within a UTC hour window for extra safety.
    You can adjust via TRADE_START_UTC_HOUR / TRADE_END_UTC_HOUR env vars.
    """
    now = datetime.utcnow()
    h = now.hour
    allowed = TRADE_START_UTC_HOUR <= h < TRADE_END_UTC_HOUR
    if not allowed:
        log(f"[TIME GUARD] Outside trading window (UTC hour={h}), no actions.")
    return allowed

# =====================================================================
# POSITION CHECKS
# =====================================================================

def has_open_position(symbol):
    """
    Check if there is any open position for the given symbol.
    Used to prevent sending SELLs when there is no position
    and to prevent duplicate BUYs.
    """
    try:
        pos = trading_client.get_open_position(symbol)
        qty = float(pos.qty)
        log(f"[POSITION] Open position detected for {symbol}: qty={qty}")
        return qty != 0.0
    except Exception:
        # No open position or error (treated as flat)
        log(f"[POSITION] No open position for {symbol}.")
        return False

# =====================================================================
# PARSE WEBHOOK JSON
# =====================================================================

def parse_webhook_payload(req):
    """
    Safely decode ANY incoming body as JSON.
    """
    try:
        raw = req.data.decode("utf-8").strip()
        log(f"RAW BODY: {raw}")

        payload = json.loads(raw)
        log(f"PARSED: {payload}")
        return payload

    except Exception as e:
        log(f"[ERROR] JSON decode failed: {e}")
        traceback.print_exc()
        return None

# =====================================================================
# SEND LIMIT ORDER (with retries, safety)
# =====================================================================

def submit_limit_order(symbol, qty, price, side):
    """
    Sends a limit order with a 0.01 buffer for instant fill attempt.
      - BUY: price + 0.01
      - SELL: price - 0.01
    Includes:
      - basic validation
      - retry logic
    """

    try:
        price = round(float(price), 4)
    except Exception:
        log(f"[ORDER ERROR] Invalid price: {price}")
        return None

    if price <= 0:
        log(f"[ORDER ERROR] Refusing to send order with non-positive price: {price}")
        return None

    if qty <= 0:
        log(f"[ORDER ERROR] Refusing to send order with non-positive qty: {qty}")
        return None

    if side == OrderSide.BUY:
        limit_price = round(price + 0.01, 4)
    else:
        limit_price = round(price - 0.01, 4)

    log(f"Preparing {side} LIMIT order for {symbol} at {limit_price} (base={price})")

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
            log(f"[ORDER SUCCESS] Attempt {attempt}: {order}")
            return order

        except Exception as e:
            log(f"[ORDER ERROR] Attempt {attempt} failed: {e}")
            traceback.print_exc()
            time.sleep(1)  # short backoff

    log("[ORDER ERROR] All retry attempts failed, giving up.")
    return None

# =====================================================================
# QUOTE / LIQUIDITY HELPER
# =====================================================================

def get_latest_bid_ask(symbol):
    """
    Returns (bid, ask, spread_pct).
    Uses Alpaca's StockHistoricalDataClient for the latest quote.
    Adds a fallback when ask == 0 but bid > 0, which can happen
    with thin liquidity or limited data subscriptions.
    """
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        latest = data_client.get_stock_latest_quote(req)

        # If symbol_or_symbols is a string, response is a dict { symbol: Quote }
        quote = latest[symbol] if isinstance(latest, dict) else latest

        bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
        ask = float(getattr(quote, "ask_price", 0.0) or 0.0)

        # If both are junk, bail
        if bid <= 0 and ask <= 0:
            log(f"[QUOTE] Both bid/ask invalid for {symbol}: bid={bid}, ask={ask}")
            return None, None, None

        # If bid is valid but ask is 0, synthesize a tiny ask above bid
        if bid > 0 and ask <= 0:
            synthetic_ask = round(bid + 0.01, 4)
            log(f"[QUOTE-FIX] ask was 0 for {symbol}, using synthetic ask={synthetic_ask} from bid={bid}")
            ask = synthetic_ask

        spread = ask - bid
        spread_pct = (spread / ask) * 100 if ask > 0 else None

        log(f"[QUOTE] {symbol} bid={bid}, ask={ask}, spread={spread:.4f} ({spread_pct:.2f}%)")
        return bid, ask, spread_pct

    except Exception as e:
        log(f"[ERROR] Quote failure: {e}")
        traceback.print_exc()
        return None, None, None

# =====================================================================
# ORDER EXECUTION LOGIC
# =====================================================================

def monitor_price():
    """
    Continuous trigger logic every second.
    Includes:
      - time window check
      - no duplicate buys
      - position sanity checks
      - trailing stop
      - target overrides trail (priority)
    """
    while True:
        try:
            # No active plan
            if active_plan["ticker"] is None:
                time.sleep(1)
                continue

            symbol = active_plan["ticker"]

            # Time guard: do nothing outside allowed hours
            if not is_trading_time():
                time.sleep(5)
                continue

            bid, ask, spread_pct = get_latest_bid_ask(symbol)
            if bid is None or ask is None:
                time.sleep(1)
                continue

            # =============================================================
            # ENTRY LOGIC
            # =============================================================
            if not active_plan["entry_filled"]:
                # If Alpaca already shows a position, mark as in position (no duplicate BUY)
                if has_open_position(symbol):
                    log("[ENTRY GUARD] Position already open; not sending another BUY.")
                    active_plan["entry_filled"] = True
                    active_plan["in_position"] = True
                else:
                    # Spread / liquidity check
                    if spread_pct is not None and spread_pct > MAX_SPREAD_PCT:
                        log(f"[SPREAD GUARD] Spread {spread_pct:.2f}% > max {MAX_SPREAD_PCT}%, entry paused.")
                    else:
                        if bid >= active_plan["entry"]:
                            log("ENTRY TRIGGERED → sending BUY")

                            order = submit_limit_order(
                                symbol=symbol,
                                qty=active_plan["qty"],
                                price=active_plan["entry"],
                                side=OrderSide.BUY
                            )

                            if order is not None:
                                active_plan["entry_filled"] = True
                                active_plan["in_position"] = True
                                active_plan["highest_bid"] = bid
                                active_plan["trail_active"] = (
                                    active_plan["trail_pct"] is not None and
                                    active_plan["trail_pct"] > 0
                                )
                                log(f"Entry filled flag set TRUE, trail_active={active_plan['trail_active']}")
                            else:
                                log("[ENTRY ERROR] Buy order failed; will retry on next ticks.")

            # If we are not in position, no need to check exits
            if not active_plan["in_position"]:
                time.sleep(1)
                continue

            # Additional sanity check: if Alpaca position disappeared, reset in_position
            if not has_open_position(symbol):
                log("[POSITION SYNC] Marking flat since no open position detected at Alpaca.")
                active_plan["in_position"] = False
                time.sleep(1)
                continue

            # =============================================================
            # EXIT LOGIC PRIORITY:
            # 1) TARGET
            # 2) HARD STOP
            # 3) TRAILING STOP
            # =============================================================

            # 1) TARGET EXIT — overrides trailing if both hit on same tick
            if active_plan["entry_filled"] and not active_plan["target_sent"]:
                if bid >= active_plan["target"]:
                    log("TARGET HIT → sending SELL")

                    if has_open_position(symbol):
                        order = submit_limit_order(
                            symbol=symbol,
                            qty=active_plan["qty"],
                            price=active_plan["target"],
                            side=OrderSide.SELL
                        )
                        if order is not None:
                            active_plan["target_sent"] = True
                            active_plan["in_position"] = False
                            active_plan["trail_active"] = False
                            log("Target sent, position closed. (Other exits logically cancelled)")
                    else:
                        log("[TARGET EXIT] No open position at Alpaca, skipping SELL.")

            # If we closed on target this loop, skip further logic
            if not active_plan["in_position"]:
                time.sleep(1)
                continue

            # 2) HARD STOP EXIT
            if active_plan["entry_filled"] and not active_plan["stop_sent"]:
                if bid <= active_plan["stop"]:
                    log("STOP HIT → sending SELL")

                    if has_open_position(symbol):
                        order = submit_limit_order(
                            symbol=symbol,
                            qty=active_plan["qty"],
                            price=active_plan["stop"],
                            side=OrderSide.SELL
                        )
                        if order is not None:
                            active_plan["stop_sent"] = True
                            active_plan["in_position"] = False
                            active_plan["trail_active"] = False
                            log("Stop sent, position closed. (Other exits logically cancelled)")
                    else:
                        log("[STOP EXIT] No open position at Alpaca, skipping SELL.")

            # If we closed on stop this loop, skip further logic
            if not active_plan["in_position"]:
                time.sleep(1)
                continue

            # 3) TRAILING STOP EXIT
            if active_plan["entry_filled"] and active_plan["trail_active"] and not active_plan["trail_sent"]:
                trail_pct = active_plan["trail_pct"]
                if trail_pct is None or trail_pct <= 0:
                    # No trail configured
                    active_plan["trail_active"] = False
                else:
                    # Update highest bid
                    if active_plan["highest_bid"] is None or bid > active_plan["highest_bid"]:
                        active_plan["highest_bid"] = bid

                    highest = active_plan["highest_bid"]
                    trail_level = highest * (1.0 - trail_pct / 100.0)

                    log(f"[TRAIL] highest={highest:.4f}, trail_level={trail_level:.4f}, bid={bid:.4f}")

                    if bid <= trail_level:
                        log("TRAILING STOP HIT → sending SELL")

                        if has_open_position(symbol):
                            # trail exits at current bid
                            order = submit_limit_order(
                                symbol=symbol,
                                qty=active_plan["qty"],
                                price=bid,
                                side=OrderSide.SELL
                            )
                            if order is not None:
                                active_plan["trail_sent"] = True
                                active_plan["in_position"] = False
                                active_plan["trail_active"] = False
                                log("Trailing stop sent, position closed. (Other exits logically cancelled)")
                        else:
                            log("[TRAIL EXIT] No open position at Alpaca, skipping SELL.")

            time.sleep(1)

        except Exception as loop_err:
            log(f"[MONITOR ERROR] {loop_err}")
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

    # Validate secret
    if str(payload.get("secret")) != str(WEBHOOK_SECRET):
        log("[ERROR] SECRET INVALID")
        return jsonify({"status": "error", "message": "bad_secret"}), 401

    log("[SECRET] VALID")

    # Extract and validate fields
    try:
        ticker = str(payload["ticker"]).upper()
        qty = int(payload["quantity"])
        entry = float(payload["entry"])
        stop = float(payload["stop"])
        target = float(payload["target"])
        trail_pct = float(payload.get("trail_pct", 0))

        if qty <= 0:
            raise ValueError("Quantity must be > 0")
        if entry <= 0 or stop <= 0 or target <= 0:
            raise ValueError("Prices must be > 0")
        if not (stop < entry < target):
            log(f"[PAYLOAD WARNING] stop={stop}, entry={entry}, target={target} look unusual.")

    except Exception as e:
        log(f"[PAYLOAD ERROR] {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": "bad_payload"}), 400

    # Store plan (reset all flags)
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
# RUN FLASK
# =====================================================================

if __name__ == "__main__":
    log(
        f"SERVER STARTED — LIVE_MODE={LIVE_MODE}, "
        f"TRADE_UTC_WINDOW={TRADE_START_UTC_HOUR}-{TRADE_END_UTC_HOUR}, "
        f"MAX_SPREAD_PCT={MAX_SPREAD_PCT}"
    )
    app.run(host="0.0.0.0", port=8080)






































































































