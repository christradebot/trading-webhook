# main.py
# Chris + Athena 2025 - Ladder Entries + Ladder Exits (no bid/ask gating)

import os
import json
import time
import threading
import traceback
from datetime import datetime, timezone

from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from alpaca.data.historical.stock import StockHistoricalDataClient, StockLatestTradeRequest

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

API_KEY = os.getenv("APCA_API_KEY_ID")
API_SECRET = os.getenv("APCA_API_SECRET_KEY")
PAPER = os.getenv("APCA_PAPER", "true").lower() == "true"

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")

# Alpaca clients
trading_client = TradingClient(API_KEY, API_SECRET, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, API_SECRET)

# Global plan state (single-ticker plan)
active_plan = {
    "ticker": None,
    "qty": 0,
    "entry": None,
    "stop": None,
    "target": None,
    "trail_pct": None,

    "in_position": False,
    "entry_filled": False,

    # ENTRY LADDER
    "entry_triggered": False,
    "entry_ladder_start": None,
    "entry_step": 0,
    "entry_order_id": None,

    # STOP LADDER
    "stop_triggered": False,
    "stop_ladder_start": None,
    "stop_step": 0,
    "stop_order_id": None,

    # TARGET LADDER
    "target_triggered": False,
    "target_ladder_start": None,
    "target_step": 0,
    "target_order_id": None,

    # TRAIL
    "trail_active": False,
    "highest_price": None,
    "trail_order_id": None,
}

plan_lock = threading.Lock()

app = Flask(__name__)


# ─────────────────────────────────────────────
# Utility logging
# ─────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ─────────────────────────────────────────────
# Market data: latest trade price ONLY
# ─────────────────────────────────────────────

def get_last_price(symbol: str) -> float | None:
    """Use Alpaca stock latest trade as the only price source."""
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        resp = data_client.get_stock_latest_trade(req)

        # resp can be a dict or a single Trade object
        trade = resp[symbol] if isinstance(resp, dict) else resp
        price = float(trade.price)
        log(f"[PRICE] {symbol} last trade price = {price:.4f}")
        return price
    except Exception as e:
        log(f"[ERROR] get_last_price({symbol}) failed: {e}")
        traceback.print_exc()
        return None


# ─────────────────────────────────────────────
# Trading helpers
# ─────────────────────────────────────────────

def has_open_position(symbol: str) -> bool:
    try:
        pos = trading_client.get_open_position(symbol)
        qty = float(pos.qty)
        in_pos = qty > 0
        log(f"[POSITION] {symbol} open qty = {qty} (in_position={in_pos})")
        return in_pos
    except Exception:
        # No open position or error means treat as flat
        log(f"[POSITION] No open position for {symbol}.")
        return False


def submit_limit_order(symbol: str, qty: int, side: OrderSide, limit_price: float):
    """Submit a simple limit order. No bid/ask logic, no spread checks."""
    try:
        if qty <= 0:
            log(f"[ORDER] Not submitting order for {symbol}: qty <= 0")
            return None

        limit_price = round(float(limit_price), 4)
        if limit_price <= 0:
            log(f"[ORDER] Not submitting order for {symbol}: invalid limit {limit_price}")
            return None

        log(f"[ORDER] {side.name} {qty} {symbol} @ {limit_price:.4f} (LIMIT, DAY)")
        order_req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=limit_price,
            time_in_force=TimeInForce.DAY
        )
        order = trading_client.submit_order(order_req)
        log(f"[ORDER] Submitted {side.name} order id={order.id}, status={order.status}")
        return order
    except Exception as e:
        log(f"[ERROR] submit_limit_order({symbol}, {side}, {limit_price}) failed: {e}")
        traceback.print_exc()
        return None


def cancel_order(order_id: str, ctx: str):
    if not order_id:
        return
    try:
        trading_client.cancel_order_by_id(order_id)
        log(f"[ORDER] Cancelled {ctx} order id={order_id}")
    except Exception as e:
        log(f"[ERROR] Cancel {ctx} order id={order_id} failed: {e}")


# ─────────────────────────────────────────────
# Webhook payload parsing
# ─────────────────────────────────────────────

def parse_webhook_payload(raw: bytes) -> dict | None:
    try:
        if not raw:
            log("[WEBHOOK] Empty body")
            return None
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        log("[WEBHOOK] JSON decode failed.")
        log(f"RAW BODY: {raw.decode('utf-8', errors='ignore')}")
        traceback.print_exc()
        return None

    log(f"[WEBHOOK] RAW BODY: {json.dumps(payload, indent=2)}")

    if payload.get("secret") != WEBHOOK_SECRET:
        log("[WEBHOOK] Invalid secret.")
        return None

    # Basic shape: PLAN instruction from TradingView
    try:
        ticker = str(payload["ticker"]).upper().strip()
        qty = int(payload["quantity"])
        entry = float(payload["entry"])
        stop = float(payload["stop"])
        target = float(payload["target"])
        trail_pct = float(payload.get("trail_pct", 0)) or 0.0
    except Exception as e:
        log(f"[WEBHOOK] Missing/invalid fields: {e}")
        return None

    return {
        "ticker": ticker,
        "qty": qty,
        "entry": entry,
        "stop": stop,
        "target": target,
        "trail_pct": trail_pct,
    }


# ─────────────────────────────────────────────
# Flask routes
# ─────────────────────────────────────────────

@app.route("/", methods=["GET"])
def health():
    return "OK", 200


@app.route("/tv", methods=["POST"])
def tv_webhook():
    raw = request.data
    plan = parse_webhook_payload(raw)
    if not plan:
        return jsonify({"status": "ignored"}), 400

    with plan_lock:
        # Load new plan
        active_plan.update({
            "ticker": plan["ticker"],
            "qty": plan["qty"],
            "entry": plan["entry"],
            "stop": plan["stop"],
            "target": plan["target"],
            "trail_pct": plan["trail_pct"],

            "in_position": has_open_position(plan["ticker"]),
            "entry_filled": False,

            "entry_triggered": False,
            "entry_ladder_start": None,
            "entry_step": 0,
            "entry_order_id": None,

            "stop_triggered": False,
            "stop_ladder_start": None,
            "stop_step": 0,
            "stop_order_id": None,

            "target_triggered": False,
            "target_ladder_start": None,
            "target_step": 0,
            "target_order_id": None,

            "trail_active": False,
            "highest_price": None,
            "trail_order_id": None,
        })

        log(f"[PLAN LOADED] {active_plan}")

    return jsonify({"status": "plan_loaded", "plan": plan}), 200


# ─────────────────────────────────────────────
# Ladder logic helpers (Option A)
# ─────────────────────────────────────────────

def compute_entry_limit(base_entry: float, step: int) -> float:
    """
    Entry ladder (BUY):
    Step 0: base_entry
    Step 1: base_entry + 0.01
    ...
    Step 5: base_entry + 0.05
    """
    if step < 0:
        step = 0
    if step > 5:
        step = 5
    return base_entry + 0.01 * step


def compute_exit_limit_from_above(base_target: float, step: int) -> float:
    """
    For TARGET SELL ladder:
    Try to get filled starting at target, then slightly more aggressive below target.

    Step 0: base_target
    Step 1: base_target - 0.01
    ...
    Step 5: base_target - 0.05
    """
    if step < 0:
        step = 0
    if step > 5:
        step = 5
    return max(0.01, base_target - 0.01 * step)


def compute_exit_limit_for_stop(base_stop: float, step: int) -> float:
    """
    For STOP SELL ladder:
    Step 0: base_stop
    Step 1: base_stop - 0.01
    ...
    Step 5: base_stop - 0.05
    """
    if step < 0:
        step = 0
    if step > 5:
        step = 5
    return max(0.01, base_stop - 0.01 * step)


# ─────────────────────────────────────────────
# Price monitoring & order engine
# ─────────────────────────────────────────────

def trade_loop():
    """Background loop that watches price and runs ladder logic."""
    log("[ENGINE] Trade loop started.")
    while True:
        try:
            with plan_lock:
                ticker = active_plan["ticker"]
                if not ticker:
                    # No active plan
                    pass
                else:
                    qty = active_plan["qty"]
                    entry = active_plan["entry"]
                    stop = active_plan["stop"]
                    target = active_plan["target"]
                    trail_pct = active_plan["trail_pct"]

                    # Get latest price (single source of truth)
                    last = get_last_price(ticker)
                    if last is None:
                        time.sleep(1)
                        continue

                    # Sync position state from Alpaca
                    in_pos = has_open_position(ticker)
                    active_plan["in_position"] = in_pos

                    # Mark entry filled if we detect a position
                    if in_pos and not active_plan["entry_filled"]:
                        log(f"[ENTRY] Position detected for {ticker}. Marking entry_filled.")
                        active_plan["entry_filled"] = True
                        active_plan["entry_triggered"] = False
                        active_plan["entry_ladder_start"] = None
                        active_plan["entry_order_id"] = None
                        active_plan["highest_price"] = last
                        active_plan["trail_active"] = trail_pct > 0

                    # Update highest price for trailing stop
                    if active_plan["entry_filled"] and in_pos:
                        hp = active_plan["highest_price"]
                        if hp is None or last > hp:
                            active_plan["highest_price"] = last

                    # ───────── ENTRY LADDER ─────────
                    if not active_plan["entry_filled"] and not in_pos:
                        # Wait for price to reach entry to start ladder
                        if not active_plan["entry_triggered"]:
                            if last >= entry:
                                active_plan["entry_triggered"] = True
                                active_plan["entry_ladder_start"] = time.time()
                                active_plan["entry_step"] = 0
                                active_plan["entry_order_id"] = None
                                log(f"[ENTRY] Triggered for {ticker} at last={last:.4f}, entry={entry:.4f}")

                        if active_plan["entry_triggered"]:
                            elapsed = time.time() - active_plan["entry_ladder_start"]
                            step = int(elapsed // 5)  # 0..5 for 0-30 seconds
                            if step > 5:
                                # Ladder time window expired -> miss trade, protect capital
                                log(f"[ENTRY] Ladder expired for {ticker} (no fill). Missed trade, protecting capital.")
                                # Cancel any outstanding entry order
                                if active_plan["entry_order_id"]:
                                    cancel_order(active_plan["entry_order_id"], "entry")
                                # Reset plan but keep ticker None to avoid accidental re-use
                                active_plan["ticker"] = None
                                active_plan["entry_triggered"] = False
                                active_plan["entry_ladder_start"] = None
                                active_plan["entry_order_id"] = None
                            else:
                                # Step change: cancel previous ladder order
                                if step != active_plan["entry_step"]:
                                    log(f"[ENTRY] Step change {active_plan['entry_step']} -> {step} for {ticker}")
                                    if active_plan["entry_order_id"]:
                                        cancel_order(active_plan["entry_order_id"], "entry")
                                        active_plan["entry_order_id"] = None
                                    active_plan["entry_step"] = step

                                # If we don't yet have an order for this step, submit one
                                if active_plan["entry_order_id"] is None:
                                    limit_price = compute_entry_limit(entry, step)
                                    log(f"[ENTRY] Step {step} BUY ladder for {ticker} @ {limit_price:.4f}")
                                    order = submit_limit_order(
                                        ticker,
                                        qty,
                                        OrderSide.BUY,
                                        limit_price
                                    )
                                    if order:
                                        active_plan["entry_order_id"] = order.id

                    # ───────── EXIT LOGIC (only if in position) ─────────
                    if active_plan["entry_filled"] and in_pos:
                        # Priority: TARGET ladder > STOP ladder > TRAIL

                        # TARGET ladder
                        if not active_plan["target_triggered"] and last >= target:
                            active_plan["target_triggered"] = True
                            active_plan["target_ladder_start"] = time.time()
                            active_plan["target_step"] = 0
                            active_plan["target_order_id"] = None
                            log(f"[TARGET] Triggered for {ticker} at last={last:.4f}, target={target:.4f}")

                        if active_plan["target_triggered"]:
                            elapsed = time.time() - active_plan["target_ladder_start"]
                            step = int(elapsed // 5)
                            if step > 5:
                                log(f"[TARGET] Ladder expired for {ticker} (no fill).")
                                if active_plan["target_order_id"]:
                                    cancel_order(active_plan["target_order_id"], "target")
                                active_plan["target_triggered"] = False
                                active_plan["target_ladder_start"] = None
                                active_plan["target_order_id"] = None
                            else:
                                if step != active_plan["target_step"]:
                                    log(f"[TARGET] Step change {active_plan['target_step']} -> {step} for {ticker}")
                                    if active_plan["target_order_id"]:
                                        cancel_order(active_plan["target_order_id"], "target")
                                        active_plan["target_order_id"] = None
                                    active_plan["target_step"] = step

                                if active_plan["target_order_id"] is None:
                                    limit_price = compute_exit_limit_from_above(target, step)
                                    log(f"[TARGET] Step {step} SELL ladder for {ticker} @ {limit_price:.4f}")
                                    order = submit_limit_order(
                                        ticker,
                                        qty,
                                        OrderSide.SELL,
                                        limit_price
                                    )
                                    if order:
                                        active_plan["target_order_id"] = order.id

                        # If target ladder is running, don't run stop/trail to avoid conflicting exits
                        if active_plan["target_triggered"]:
                            pass
                        else:
                            # STOP ladder
                            if not active_plan["stop_triggered"] and last <= stop:
                                active_plan["stop_triggered"] = True
                                active_plan["stop_ladder_start"] = time.time()
                                active_plan["stop_step"] = 0
                                active_plan["stop_order_id"] = None
                                log(f"[STOP] Triggered for {ticker} at last={last:.4f}, stop={stop:.4f}")

                            if active_plan["stop_triggered"]:
                                elapsed = time.time() - active_plan["stop_ladder_start"]
                                step = int(elapsed // 5)
                                if step > 5:
                                    log(f"[STOP] Ladder expired for {ticker} (no fill). We tried aggressively; capital at risk reduced as much as possible.")
                                    if active_plan["stop_order_id"]:
                                        cancel_order(active_plan["stop_order_id"], "stop")
                                    active_plan["stop_triggered"] = False
                                    active_plan["stop_ladder_start"] = None
                                    active_plan["stop_order_id"] = None
                                else:
                                    if step != active_plan["stop_step"]:
                                        log(f"[STOP] Step change {active_plan['stop_step']} -> {step} for {ticker}")
                                        if active_plan["stop_order_id"]:
                                            cancel_order(active_plan["stop_order_id"], "stop")
                                            active_plan["stop_order_id"] = None
                                        active_plan["stop_step"] = step

                                    if active_plan["stop_order_id"] is None:
                                        limit_price = compute_exit_limit_for_stop(stop, step)
                                        log(f"[STOP] Step {step} SELL ladder for {ticker} @ {limit_price:.4f}")
                                        order = submit_limit_order(
                                            ticker,
                                            qty,
                                            OrderSide.SELL,
                                            limit_price
                                        )
                                        if order:
                                            active_plan["stop_order_id"] = order.id

                            # TRAILING STOP (simple version – only used if neither target nor stop ladder is active)
                            if (not active_plan["target_triggered"]
                                and not active_plan["stop_triggered"]
                                and active_plan["trail_pct"] > 0):
                                hp = active_plan["highest_price"]
                                if hp is not None:
                                    trail_level = hp * (1.0 - active_plan["trail_pct"] / 100.0)
                                    if last <= trail_level and not active_plan["trail_order_id"]:
                                        # One-shot trail exit,
                                        # still as a LIMIT order a bit below last (no market orders).
                                        limit_price = max(0.01, last - 0.01)
                                        log(f"[TRAIL] SELL {ticker} trail hit at last={last:.4f}, "
                                            f"hp={hp:.4f}, level={trail_level:.4f}, "
                                            f"limit={limit_price:.4f}")
                                        order = submit_limit_order(
                                            ticker,
                                            qty,
                                            OrderSide.SELL,
                                            limit_price
                                        )
                                        if order:
                                            active_plan["trail_order_id"] = order.id

        except Exception as e:
            log(f"[ENGINE ERROR] {e}")
            traceback.print_exc()

        time.sleep(1)


# Start engine thread when the app module is imported (Railway + gunicorn friendly)
engine_thread = threading.Thread(target=trade_loop, daemon=True)
engine_thread.start()


# For local debugging
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))








































































































