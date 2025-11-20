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

# Live trading (paper=False if LIVE_MODE=True)
LIVE_MODE = True

# Spread safety (percentage, e.g. 5 = 5%)
MAX_SPREAD_PCT = float(os.environ.get("MAX_SPREAD_PCT", "5"))

# Allowed trading hours in UTC (24h format)
TRADE_START_UTC_HOUR = int(os.environ.get("TRADE_START_UTC_HOUR", "4"))   # 04:00 UTC
TRADE_END_UTC_HOUR   = int(os.environ.get("TRADE_END_UTC_HOUR", "20"))    # 20:00 UTC

# Order retry attempts
MAX_ORDER_ATTEMPTS = 3

if not all([API_KEY, SECRET_KEY, BASE_URL]):
    print("[FATAL] Missing Alpaca API credentials.")
    exit(1)

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=not LIVE_MODE)

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

def log(msg: str):
    print(f"[{datetime.utcnow()}] {msg}", flush=True)

# =====================================================================
# TIME WINDOW CHECK
# =====================================================================

def is_trading_time() -> bool:
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

def has_open_position(symbol: str) -> bool:
    """
    Check if there is any open position for the given symbol.
    Used to prevent sending SELLs when there is no position, and
    to confirm that exits actually closed the position.
    """
    try:
        pos = trading_client.get_open_position(symbol)
        qty = float(pos.qty)
        log(f"[POSITION] Open position detected for {symbol}: qty={qty}")
        return qty != 0
    except Exception:
        log(f"[POSITION] No open position for {symbol}.")
        return False

# =====================================================================
# PARSE WEBHOOK JSON
# =====================================================================

def parse_webhook_payload(req):
    """
    Safely decode ANY incoming body as JSON.
    Non-JSON payloads (e.g. "STEC Crossing 1.15") are just logged & rejected.
    """
    try:
        raw = req.data.decode("utf-8").strip()
        log(f"RAW BODY: {raw}")

        payload = json.loads(raw)
        log(f"PARSED: {payload}")
        return payload

    except Exception as e:
        log(f"[ERROR] JSON decode failed.")
        traceback.print_exc()
        return None

# =====================================================================
# PRICE / QUOTE HELPER
# =====================================================================

def get_latest_bid_ask(symbol):
    """
    Returns (bid, ask, spread_pct).
    If data is missing, returns (None, None, None).
    Uses TradingClient.get_latest_quote (returns a Quote object).
    """
    try:
        quote = trading_client.get_latest_quote(symbol)

        # Quote object with attributes like bid_price / ask_price
        bid = float(quote.bid_price) if quote.bid_price is not None else 0.0
        ask = float(quote.ask_price) if quote.ask_price is not None else 0.0

        # Handle missing or zero ask: synthetic ask slightly above bid
        if ask <= 0 and bid > 0:
            ask = round(bid + 0.01, 2)
            log(f"[QUOTE-FIX] ask was 0 for {symbol}, using synthetic ask={ask} from bid={bid}")

        if bid <= 0 or ask <= 0:
            log(f"[QUOTE] Invalid bid/ask for {symbol}: bid={bid}, ask={ask}")
            return None, None, None

        spread = ask - bid
        spread_pct = (spread / ask) * 100 if ask > 0 else None

        log(f"[QUOTE] {symbol} bid={bid}, ask={ask}, spread={spread:.4f} ({spread_pct:.2f}%)")
        return bid, ask, spread_pct

    except Exception as e:
        log(f"[ERROR] Quote failure: {e}")
        traceback.print_exc()
        return None, None, None

# =====================================================================
# PRICE HELPERS FOR BLIND LIMITS
# =====================================================================

def calc_entry_limit(entry_price: float) -> float:
    """
    Blind BUY limit:
      - Above $1 : +0.3% (entry * 1.003)
      - Below $1 : +$0.003
    """
    if entry_price >= 1.0:
        return round(entry_price * 1.003, 4)
    else:
        return round(entry_price + 0.003, 4)

def calc_target_limit(target_price: float) -> float:
    """
    Target SELL limit with small cushion to ensure fill:
      - Above $1 : -0.3% (target * 0.997)
      - Below $1 : -$0.003
    """
    if target_price >= 1.0:
        return round(target_price * 0.997, 4)
    else:
        return round(target_price - 0.003, 4)

def calc_trail_exit_limit(bid_price: float) -> float:
    """
    Trailing stop SELL at/just below current bid to exit quickly:
      - Above $1 : bid * 0.997
      - Below $1 : bid - $0.003
    """
    if bid_price >= 1.0:
        return round(bid_price * 0.997, 4)
    else:
        return round(bid_price - 0.003, 4)

def calc_stop_limit(stop_price: float, stage: int) -> float:
    """
    Two-stage hard stop SELL:
      Stage 1 (tight):
        - Above $1 : stop * 0.997
        - Below $1 : stop - $0.003
      Stage 2 (panic):
        - Above $1 : stop * 0.99
        - Below $1 : stop - $0.01
    """
    if stage == 1:
        if stop_price >= 1.0:
            return round(stop_price * 0.997, 4)
        else:
            return round(stop_price - 0.003, 4)
    else:  # stage 2
        if stop_price >= 1.0:
            return round(stop_price * 0.99, 4)
        else:
            return round(stop_price - 0.01, 4)

# =====================================================================
# RAW LIMIT ORDER SENDER (no extra price logic here)
# =====================================================================

def submit_limit_order(symbol: str, qty: int, limit_price: float, side: OrderSide):
    """
    Sends a limit order as-is (blind limit).
    Includes basic validation and retry logic.
    """

    try:
        limit_price = round(float(limit_price), 4)
    except Exception:
        log(f"[ORDER ERROR] Invalid price: {limit_price}")
        return None

    if limit_price <= 0:
        log(f"[ORDER ERROR] Refusing to send order with non-positive price: {limit_price}")
        return None

    if qty <= 0:
        log(f"[ORDER ERROR] Refusing to send order with non-positive qty: {qty}")
        return None

    log(f"[ORDER PREP] {side} LIMIT {symbol} @ {limit_price} qty={qty}")

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
            time.sleep(1)

    log("[ORDER ERROR] All retry attempts failed, giving up.")
    return None

# =====================================================================
# ORDER EXECUTION LOGIC
# =====================================================================

def monitor_price():
    """
    Continuous trigger logic every second.
    Includes:
      - Time window check
      - No duplicate buys if a position already exists
      - Spread / liquidity guard
      - Two-stage hard stop
      - Target exit
      - Trailing stop exit (lowest priority)
    """
    while True:
        try:
            if active_plan["ticker"] is None:
                time.sleep(1)
                continue

            symbol = active_plan["ticker"]

            # Time guard
            if not is_trading_time():
                time.sleep(5)
                continue

            bid, ask, spread_pct = get_latest_bid_ask(symbol)
            if bid is None or ask is None:
                time.sleep(1)
                continue

            # =========================================================
            # ENTRY LOGIC
            # =========================================================
            if not active_plan["entry_filled"]:
                # Skip if we already have a position (no duplicate buys)
                if has_open_position(symbol):
                    log("[ENTRY GUARD] Position already open; not sending another BUY.")
                    active_plan["entry_filled"] = True
                    active_plan["in_position"] = True
                else:
                    # Spread guard
                    if spread_pct is not None and spread_pct > MAX_SPREAD_PCT:
                        log(f"[SPREAD GUARD] Spread {spread_pct:.2f}% > max {MAX_SPREAD_PCT}%, entry paused.")
                    else:
                        # Trigger only once price >= entry
                        if bid >= active_plan["entry"]:
                            log("[ENTRY] Triggered → sending blind BUY")

                            buy_limit = calc_entry_limit(active_plan["entry"])
                            order = submit_limit_order(
                                symbol=symbol,
                                qty=active_plan["qty"],
                                limit_price=buy_limit,
                                side=OrderSide.BUY
                            )

                            if order is not None:
                                # Small wait then confirm via position
                                time.sleep(0.6)
                                if has_open_position(symbol):
                                    active_plan["entry_filled"] = True
                                    active_plan["in_position"] = True
                                    active_plan["highest_bid"] = bid
                                    active_plan["trail_active"] = (
                                        active_plan["trail_pct"] is not None
                                        and active_plan["trail_pct"] > 0
                                    )
                                    log(f"[ENTRY] Confirmed, trail_active={active_plan['trail_active']}")
                                else:
                                    log("[ENTRY WARNING] Buy order sent but no position detected yet.")
                            else:
                                log("[ENTRY ERROR] Buy order failed; will retry on future ticks.")

            # If we are not in a position, no need to check exits
            if not active_plan["in_position"]:
                time.sleep(1)
                continue

            # Extra sync: if Alpaca position disappeared, mark flat
            if not has_open_position(symbol):
                log("[POSITION SYNC] No open position found; marking flat.")
                active_plan["in_position"] = False
                time.sleep(1)
                continue

            # =========================================================
            # EXIT PRIORITY:
            #   1) TARGET
            #   2) HARD STOP (two-stage)
            #   3) TRAILING STOP
            # =========================================================

            # 1) TARGET EXIT
            if active_plan["entry_filled"] and not active_plan["target_sent"]:
                if bid >= active_plan["target"]:
                    log("[TARGET] Hit → sending SELL")

                    if has_open_position(symbol):
                        target_limit = calc_target_limit(active_plan["target"])
                        order = submit_limit_order(
                            symbol=symbol,
                            qty=active_plan["qty"],
                            limit_price=target_limit,
                            side=OrderSide.SELL
                        )
                        if order is not None:
                            time.sleep(0.6)
                            if not has_open_position(symbol):
                                active_plan["target_sent"] = True
                                active_plan["in_position"] = False
                                active_plan["trail_active"] = False
                                log("[TARGET] Confirmed filled, position closed.")
                            else:
                                log("[TARGET WARNING] Target SELL sent but position still open.")
                    else:
                        log("[TARGET] No open position at Alpaca, skipping SELL.")

            # If closed by target, skip further logic
            if not active_plan["in_position"]:
                time.sleep(1)
                continue

            # 2) HARD STOP EXIT (Two-stage)
            if active_plan["entry_filled"] and not active_plan["stop_sent"]:
                if bid <= active_plan["stop"]:
                    log("[STOP] Hit → sending Stage 1 SELL")

                    if has_open_position(symbol):
                        # Stage 1: tight limit
                        stop_limit_stage1 = calc_stop_limit(active_plan["stop"], stage=1)
                        order1 = submit_limit_order(
                            symbol=symbol,
                            qty=active_plan["qty"],
                            limit_price=stop_limit_stage1,
                            side=OrderSide.SELL
                        )

                        if order1 is not None:
                            time.sleep(0.6)
                            if not has_open_position(symbol):
                                active_plan["stop_sent"] = True
                                active_plan["in_position"] = False
                                active_plan["trail_active"] = False
                                log("[STOP] Stage 1 filled, position closed.")
                            else:
                                # Stage 2: panic limit
                                log("[STOP] Stage 1 not fully filled → Stage 2 panic SELL")
                                stop_limit_stage2 = calc_stop_limit(active_plan["stop"], stage=2)
                                order2 = submit_limit_order(
                                    symbol=symbol,
                                    qty=active_plan["qty"],
                                    limit_price=stop_limit_stage2,
                                    side=OrderSide.SELL
                                )
                                if order2 is not None:
                                    time.sleep(1.0)
                                    if not has_open_position(symbol):
                                        active_plan["stop_sent"] = True
                                        active_plan["in_position"] = False
                                        active_plan["trail_active"] = False
                                        log("[STOP] Stage 2 filled, position closed.")
                                    else:
                                        log("[STOP WARNING] Stage 2 SELL sent but position still open; will keep managing.")
                                else:
                                    log("[STOP ERROR] Stage 2 order failed to send.")
                        else:
                            log("[STOP ERROR] Stage 1 order failed to send; will retry if stop condition persists.")
                    else:
                        log("[STOP] No open position at Alpaca, skipping SELL.")

            if not active_plan["in_position"]:
                time.sleep(1)
                continue

            # 3) TRAILING STOP EXIT
            if active_plan["entry_filled"] and active_plan["trail_active"] and not active_plan["trail_sent"]:
                trail_pct = active_plan["trail_pct"]
                if trail_pct is None or trail_pct <= 0:
                    active_plan["trail_active"] = False
                else:
                    # Track highest bid since entry
                    if active_plan["highest_bid"] is None or bid > active_plan["highest_bid"]:
                        active_plan["highest_bid"] = bid

                    highest = active_plan["highest_bid"]
                    trail_level = highest * (1.0 - trail_pct / 100.0)

                    log(f"[TRAIL] highest={highest:.4f}, trail_level={trail_level:.4f}, bid={bid:.4f}")

                    if bid <= trail_level:
                        log("[TRAIL] Hit → sending SELL")

                        if has_open_position(symbol):
                            # Exit near current bid with a small cushion
                            trail_limit = calc_trail_exit_limit(bid)
                            order = submit_limit_order(
                                symbol=symbol,
                                qty=active_plan["qty"],
                                limit_price=trail_limit,
                                side=OrderSide.SELL
                            )
                            if order is not None:
                                time.sleep(0.8)
                                if not has_open_position(symbol):
                                    active_plan["trail_sent"] = True
                                    active_plan["in_position"] = False
                                    active_plan["trail_active"] = False
                                    log("[TRAIL] Filled, position closed.")
                                else:
                                    log("[TRAIL WARNING] Trailing SELL sent but position still open.")
                        else:
                            log("[TRAIL] No open position at Alpaca, skipping SELL.")

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

    # Extract & validate fields
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

    # Store plan & reset flags
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







































































































