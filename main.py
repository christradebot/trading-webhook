import os
import json
import time
import traceback
from datetime import datetime
from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# =====================================================================
# CONFIG
# =====================================================================

API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
BASE_URL = os.environ.get("APCA_API_BASE_URL")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

# Live vs paper trading
# LIVE_MODE = True  → real account
# LIVE_MODE = False → paper trading
LIVE_MODE = True

# Spread safety (percentage, e.g. 5 = 5%)
MAX_SPREAD_PCT = float(os.environ.get("MAX_SPREAD_PCT", "5"))

# Allowed trading hours in UTC (24h)
TRADE_START_UTC_HOUR = int(os.environ.get("TRADE_START_UTC_HOUR", "4"))   # 04:00 UTC
TRADE_END_UTC_HOUR   = int(os.environ.get("TRADE_END_UTC_HOUR", "20"))   # 20:00 UTC

# Order retry attempts
MAX_ORDER_ATTEMPTS = 3

# Entry ladder config
ENTRY_LADDER_STEP_SECONDS = 5        # each stage lasts 5 seconds
ENTRY_LADDER_MAX_STAGE = 5           # 0..5 → total 6 stages
ENTRY_LADDER_STEP_CENTS = 0.01       # each stage adds 0.01 above entry

if not all([API_KEY, SECRET_KEY, BASE_URL]):
    print("[FATAL] Missing Alpaca API credentials.", flush=True)
    raise SystemExit(1)

# Alpaca trading client
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
    "highest_bid": None,

    # entry ladder state
    "entry_ladder_started": False,
    "entry_ladder_start_ts": None,
    "entry_ladder_stage": -1,  # last stage we sent an order for
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
    Prevents duplicate buys / stray sells.
    """
    try:
        pos = trading_client.get_open_position(symbol)
        qty = float(pos.qty)
        log(f"[POSITION] Open position detected for {symbol}: qty={qty}")
        return qty != 0
    except Exception:
        # No open position or error → treat as flat
        log(f"[POSITION] No open position for {symbol}.")
        return False

# =====================================================================
# WEBHOOK PAYLOAD PARSING
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
        log(f"[ERROR] JSON decode failed.")
        traceback.print_exc()
        return None

# =====================================================================
# ORDER HELPERS
# =====================================================================

def submit_limit_order(symbol: str, qty: int, price: float, side: OrderSide):
    """
    Sends a LIMIT order with simple validation & retries.
    We pass the desired limit price directly (no internal extra buffer).
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

    log(f"[ORDER] Preparing {side.name} LIMIT for {symbol} at {price}")

    for attempt in range(1, MAX_ORDER_ATTEMPTS + 1):
        try:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                limit_price=price,
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


def submit_market_order(symbol: str, qty: int, side: OrderSide):
    """
    Sends a MARKET order – used for 'must-exit' scenarios if desired.
    (Currently unused; here for future extension.)
    """
    if qty <= 0:
        log(f"[MARKET ORDER ERROR] Non-positive qty: {qty}")
        return None

    log(f"[MARKET ORDER] {side.name} {qty} {symbol} at MARKET")
    try:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            time_in_force=TimeInForce.DAY
        )
        order = trading_client.submit_order(req)
        log(f"[MARKET ORDER SUCCESS] {order}")
        return order
    except Exception as e:
        log(f"[MARKET ORDER ERROR] {e}")
        traceback.print_exc()
        return None

# =====================================================================
# QUOTE / LIQUIDITY HELPER
# =====================================================================

def get_latest_bid_ask(symbol: str):
    """
    Returns (bid, ask, spread_pct).
    Uses trading_client.get_latest_quote and repairs ask=0 with a synthetic ask.
    """
    try:
        quote = trading_client.get_latest_quote(symbol)

        # Alpaca Quote object typically has .bid_price / .ask_price
        bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
        ask = float(getattr(quote, "ask_price", 0.0) or 0.0)

        # Repair zero ask with a tiny synthetic spread if we at least have a bid
        if ask <= 0 and bid > 0:
            ask = round(bid * 1.0125, 4)  # ~1.25% above bid
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
# MAIN MONITOR LOOP
# =====================================================================

def monitor_price():
    """
    Continuous trigger logic every second.
    Includes:
      - time window check
      - spread guard
      - no duplicate buys (via has_open_position)
      - laddered entry logic (0–30s, +0.01 per 5s)
      - target, stop, trailing stop
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

            now_ts = time.time()

            # -------------------------------------------------------------
            # ENTRY LADDER LOGIC
            # -------------------------------------------------------------
            if not active_plan["entry_filled"]:
                # Sync with Alpaca – if somehow a position is already open
                if has_open_position(symbol):
                    active_plan["entry_filled"] = True
                    active_plan["in_position"] = True
                    active_plan["highest_bid"] = bid
                    log("[ENTRY SYNC] Position already open; marking in_position=True.")
                else:
                    # Spread guard
                    if spread_pct is not None and spread_pct > MAX_SPREAD_PCT:
                        log(f"[SPREAD GUARD] Spread {spread_pct:.2f}% > max {MAX_SPREAD_PCT}%, entry paused.")
                    else:
                        # Only start ladder once bid touches or exceeds desired entry
                        if bid >= active_plan["entry"]:
                            if not active_plan["entry_ladder_started"]:
                                active_plan["entry_ladder_started"] = True
                                active_plan["entry_ladder_start_ts"] = now_ts
                                active_plan["entry_ladder_stage"] = -1
                                log(f"[ENTRY LADDER] Started at bid={bid}, entry={active_plan['entry']}")

                            elapsed = now_ts - (active_plan["entry_ladder_start_ts"] or now_ts)
                            stage = int(elapsed // ENTRY_LADDER_STEP_SECONDS)

                            if stage > ENTRY_LADDER_MAX_STAGE:
                                # We've given it 30s of chasing; stop trying
                                log("[ENTRY LADDER] Max stage reached with no position; giving up on this plan.")
                            else:
                                # Only send one order per stage
                                if stage > active_plan["entry_ladder_stage"]:
                                    extra = stage * ENTRY_LADDER_STEP_CENTS
                                    ladder_price = active_plan["entry"] + extra
                                    ladder_price = round(ladder_price, 4)

                                    log(
                                        f"[ENTRY LADDER] stage={stage}, "
                                        f"elapsed={elapsed:.1f}s, "
                                        f"extra={extra:.2f}, "
                                        f"limit_price={ladder_price}"
                                    )

                                    order = submit_limit_order(
                                        symbol=symbol,
                                        qty=active_plan["qty"],
                                        price=ladder_price,
                                        side=OrderSide.BUY
                                    )

                                    # Record that we’ve used this stage
                                    active_plan["entry_ladder_stage"] = stage

                                    # We *do not* mark entry_filled here.
                                    # We rely on has_open_position() in later loops
                                    # to confirm an actual position.
                                    if order is None:
                                        log("[ENTRY LADDER] Order submission failed this stage.")
                        else:
                            # Bid is still below entry – ladder not started yet
                            pass

            # If still not in a position, skip exit logic
            if not active_plan["in_position"] and not active_plan["entry_filled"]:
                time.sleep(1)
                continue

            # Sync position status – if Alpaca has no position, we’re flat
            if not has_open_position(symbol):
                if active_plan["in_position"]:
                    log("[POSITION SYNC] No open position at Alpaca; marking in_position=False.")
                active_plan["in_position"] = False

            if not active_plan["in_position"]:
                # No position, no exits required
                time.sleep(1)
                continue

            # Update highest bid for trailing stop logic
            if active_plan["highest_bid"] is None or bid > active_plan["highest_bid"]:
                active_plan["highest_bid"] = bid

            # -------------------------------------------------------------
            # EXIT PRIORITY:
            # 1) TARGET
            # 2) HARD STOP
            # 3) TRAILING STOP
            # -------------------------------------------------------------

            # 1) TARGET EXIT — overrides trailing if both conditions happen together
            if active_plan["entry_filled"] and not active_plan["target_sent"]:
                if bid >= active_plan["target"]:
                    log("[TARGET] Target hit, sending SELL.")

                    if has_open_position(symbol):
                        # Slightly aggressive: small 0.01 discount to help fill
                        target_price = round(active_plan["target"] - 0.01, 4)
                        order = submit_limit_order(
                            symbol=symbol,
                            qty=active_plan["qty"],
                            price=target_price,
                            side=OrderSide.SELL
                        )
                        if order is not None:
                            active_plan["target_sent"] = True
                            active_plan["in_position"] = False
                            active_plan["trail_active"] = False
                            log("[TARGET] Order sent, position closed (other exits implicitly cancelled).")
                    else:
                        log("[TARGET] No open position at Alpaca; skipping SELL.")

            if not active_plan["in_position"]:
                time.sleep(1)
                continue

            # 2) HARD STOP EXIT
            if active_plan["entry_filled"] and not active_plan["stop_sent"]:
                if bid <= active_plan["stop"]:
                    log("[STOP] Stop level hit, sending SELL.")

                    if has_open_position(symbol):
                        # Slightly aggressive: sell a touch below stop for better fill chance
                        stop_price = round(active_plan["stop"] - 0.01, 4)
                        order = submit_limit_order(
                            symbol=symbol,
                            qty=active_plan["qty"],
                            price=stop_price,
                            side=OrderSide.SELL
                        )
                        if order is not None:
                            active_plan["stop_sent"] = True
                            active_plan["in_position"] = False
                            active_plan["trail_active"] = False
                            log("[STOP] Order sent, position closed (other exits implicitly cancelled).")
                    else:
                        log("[STOP] No open position at Alpaca; skipping SELL.")

            if not active_plan["in_position"]:
                time.sleep(1)
                continue

            # 3) TRAILING STOP EXIT
            if (
                active_plan["entry_filled"]
                and active_plan["trail_pct"] is not None
                and active_plan["trail_pct"] > 0
                and not active_plan["trail_sent"]
            ):
                trail_pct = active_plan["trail_pct"]
                highest = active_plan["highest_bid"] or bid
                trail_level = highest * (1.0 - trail_pct / 100.0)

                log(
                    f"[TRAIL] highest={highest:.4f}, "
                    f"trail_level={trail_level:.4f}, "
                    f"bid={bid:.4f}"
                )

                if bid <= trail_level:
                    log("[TRAIL] Trailing stop hit, sending SELL.")

                    if has_open_position(symbol):
                        # Trail exits at current bid (or just under)
                        trail_price = round(bid - 0.01, 4)
                        order = submit_limit_order(
                            symbol=symbol,
                            qty=active_plan["qty"],
                            price=trail_price,
                            side=OrderSide.SELL
                        )
                        if order is not None:
                            active_plan["trail_sent"] = True
                            active_plan["in_position"] = False
                            active_plan["trail_active"] = False
                            log("[TRAIL] Order sent, position closed (other exits implicitly cancelled).")
                    else:
                        log("[TRAIL] No open position at Alpaca; skipping SELL.")

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

    # Extract + validate fields
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

    # Reset + store plan
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
        "trail_active": trail_pct > 0,
        "highest_bid": None,

        "entry_ladder_started": False,
        "entry_ladder_start_ts": None,
        "entry_ladder_stage": -1,
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







































































































