import json
import os
import time
import threading
import logging
from datetime import datetime, date
from typing import Dict, Any, Optional

from flask import Flask, request, jsonify

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest,
    GetOrdersRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import (
    OrderSide,
    TimeInForce,
    OrderClass,
    QueryOrderStatus,
)

# ============================================================
#   CONFIG / ENV VARS  (MATCH RAILWAY EXACTLY)
# ============================================================

API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
APCA_API_BASE_URL = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set")

# Alpaca clients (paper=True uses the paper endpoint given by APCA_API_BASE_URL)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ============================================================
#   BOT BEHAVIOUR
# ============================================================

# BUFFER:
#   >= 1.00  -> +0.05
#   <  1.00  -> +0.005
BUY_EXPIRY_SECONDS = 60     # 1 minute to fill, then cancel

# We allow unlimited tickers and trades, but keep 1 plan per ticker to avoid chaos
plans: Dict[str, Dict[str, Any]] = {}   # per-ticker trade plan & state

DAILY_STATS = {
    "date": date.today().isoformat(),
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "net": 0.0,
}

# Flask app
app = Flask(__name__)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("levels-bot")


# ============================================================
#   HELPERS
# ============================================================

def reset_daily_if_needed() -> None:
    """Reset daily PnL counters when the calendar day changes."""
    today = date.today().isoformat()
    if DAILY_STATS["date"] != today:
        DAILY_STATS["date"] = today
        DAILY_STATS["trades"] = 0
        DAILY_STATS["wins"] = 0
        DAILY_STATS["losses"] = 0
        DAILY_STATS["net"] = 0.0
        logger.info("🧹 New day detected; DAILY_STATS reset")


def buffer_for_entry(entry: float) -> float:
    """Return the entry buffer based on price level."""
    if entry >= 1.0:
        return 0.05
    else:
        return 0.005


def get_latest_quote(symbol: str):
    """Fetch latest quote (bid/ask) from Alpaca. Returns None on failure."""
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        resp = data_client.get_stock_latest_quote(req)
        quote = resp[symbol]
        return quote
    except Exception as e:
        logger.error("⚠️ Failed to fetch latest quote for %s: %s", symbol, e)
        return None


def compute_limit_price(entry_price: float, symbol: str) -> float:
    """
    Compute the BUY limit:
      - Base = max(entry_price, best ask) if we have it
      - Then add buffer: +0.05 or +0.005
    """
    buf = buffer_for_entry(entry_price)
    ref_price = entry_price

    quote = get_latest_quote(symbol)
    if quote:
        try:
            ask = float(quote.ask_price)
            if ask > 0:
                ref_price = max(ref_price, ask)
        except Exception:
            pass

    limit_price = round(ref_price + buf, 4)
    return limit_price


def validate_plan_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a PLAN payload from TradingView."""
    secret = payload.get("secret")
    if secret != WEBHOOK_SECRET:
        raise ValueError("Invalid secret")

    action = str(payload.get("action", "")).upper()
    if action != "PLAN":
        raise ValueError("Unsupported action (expected PLAN)")

    ticker = payload.get("ticker")
    if not ticker or not isinstance(ticker, str):
        raise ValueError("Missing ticker")

    qty = int(payload.get("quantity", 0))
    if qty <= 0:
        raise ValueError("Missing or invalid quantity")

    entry_price = float(payload.get("entry_price", 0))
    stop_price = float(payload.get("stop_price", 0))
    target_price = float(payload.get("target_price", 0))

    if entry_price <= 0 or stop_price <= 0 or target_price <= 0:
        raise ValueError("Missing or invalid entry/stop/target price")

    # For now we assume LONG only: stop below entry, target above entry
    if not (stop_price < entry_price < target_price):
        raise ValueError("Expected stop < entry < target for long trades")

    source = payload.get("source", "LEVELS")

    return {
        "ticker": ticker.upper(),
        "qty": qty,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "source": source,
    }


def start_buy_expiry_timer(symbol: str, parent_order_id: str) -> None:
    """
    Start a background timer that cancels the BUY leg if it's still unfilled
    after BUY_EXPIRY_SECONDS.
    """

    def worker():
        time.sleep(BUY_EXPIRY_SECONDS)
        plan = plans.get(symbol)
        if not plan:
            return

        # If we've already marked as filled / cancelled / closed, do nothing.
        if plan.get("status") in ("FILLED", "CANCELLED", "CLOSED"):
            return

        try:
            order = trading_client.get_order_by_id(parent_order_id)
            status = str(order.status).lower()
            # Treat NEW/ACCEPTED/PENDING_NEW as "not yet filled"
            if status in ("new", "accepted", "pending_new"):
                trading_client.cancel_order_by_id(parent_order_id)
                plan["status"] = "CANCELLED"
                logger.info("⏱️ Cancelled unfilled BUY for %s after %ds", symbol, BUY_EXPIRY_SECONDS)
            else:
                logger.info("⏱️ Expiry check: BUY for %s is status=%s (no cancel)", symbol, status)
        except Exception as e:
            logger.error("❌ Failed expiry cancel for %s: %s", symbol, e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()


def place_bracket_limit_buy(plan: Dict[str, Any]) -> Optional[str]:
    """
    Place a bracket LIMIT BUY order using:
      - entry_price (from plan) + buffer
      - target_price as take-profit limit
      - stop_price as stop-loss
    Returns parent order id on success, None on failure.
    """
    symbol = plan["ticker"]
    qty = plan["qty"]
    entry_price = plan["entry_price"]
    stop_price = plan["stop_price"]
    target_price = plan["target_price"]
    source = plan["source"]

    limit_price = compute_limit_price(entry_price, symbol)

    order_req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=target_price),
        stop_loss=StopLossRequest(stop_price=stop_price),
    )

    try:
        order = trading_client.submit_order(order_data=order_req)
        logger.info(
            "✅ PLAN BUY submitted %s x%d @ %.4f (entry=%.4f, stop=%.4f, target=%.4f) src=%s",
            symbol, qty, limit_price, entry_price, stop_price, target_price, source,
        )
        plan["parent_order_id"] = str(order.id)
        plan["limit_price"] = float(limit_price)
        plan["status"] = "SUBMITTED"
        plan["created_at"] = datetime.utcnow().isoformat()
        return str(order.id)
    except Exception as e:
        logger.error("❌ Failed to submit PLAN BUY for %s: %s", symbol, e)
        plan["status"] = "ERROR"
        return None


def reconcile_plan(symbol: str) -> None:
    """
    Reconcile a single plan:
      - Detect if parent is filled.
      - Detect if bracket children closed the trade.
      - Log PnL once when fully closed.
    """
    plan = plans.get(symbol)
    if not plan:
        return

    parent_id = plan.get("parent_order_id")
    if not parent_id:
        return

    # 1) Check if there is still an open position
    try:
        pos = trading_client.get_open_position(symbol)
        # If we get here without exception, we still have a live position
        plan["status"] = "OPEN"
        plan["last_price"] = float(pos.current_price)
        return
    except Exception:
        # No open position -> could be not yet filled or already fully closed
        pass

    # 2) Fetch all CLOSED orders for this symbol and look for children of our parent
    try:
        req = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            symbols=[symbol],
            limit=50,
        )
        orders = trading_client.get_orders(filter=req)
    except Exception as e:
        logger.error("⚠️ Failed to fetch closed orders for reconcile %s: %s", symbol, e)
        return

    # Find parent and children
    parent = None
    children = []
    for o in orders:
        oid = str(o.id)
        poid = str(o.parent_order_id) if getattr(o, "parent_order_id", None) else None
        if oid == parent_id:
            parent = o
        if poid == parent_id:
            children.append(o)

    if not parent:
        # No info yet
        return

    parent_status = str(parent.status).lower()

    # If the parent itself was cancelled / expired and never filled, mark plan and stop.
    if parent_status in ("canceled", "expired", "rejected"):
        plan["status"] = "CANCELLED"
        return

    # If there were no children filled, nothing to do.
    if not children:
        return

    # Find the last filled child (our exit)
    filled_children = [c for c in children if str(c.status).lower() == "filled"]
    if not filled_children:
        return

    exit_order = sorted(
        filled_children,
        key=lambda c: c.filled_at or datetime.min
    )[-1]

    if plan.get("pnl_logged"):
        # Already logged PnL once
        plan["status"] = "CLOSED"
        return

    # Compute PnL
    reset_daily_if_needed()

    entry_price = float(plan.get("limit_price", plan["entry_price"]))
    exit_price = float(exit_order.filled_avg_price or exit_order.limit_price or exit_order.stop_price or 0.0)
    qty = int(plan["qty"])

    if entry_price > 0 and exit_price > 0 and qty > 0:
        pnl = (exit_price - entry_price) * qty
        pnl_pct = (exit_price / entry_price - 1.0) * 100.0

        DAILY_STATS["trades"] += 1
        DAILY_STATS["net"] += pnl
        if pnl > 0:
            DAILY_STATS["wins"] += 1
        elif pnl < 0:
            DAILY_STATS["losses"] += 1

        logger.info(
            "📊 PNL %s: qty=%d entry=%.4f exit=%.4f -> $%.2f (%.2f%%)",
            symbol, qty, entry_price, exit_price, pnl, pnl_pct,
        )
        logger.info(
            "📈 DAILY SUMMARY: trades=%d wins=%d losses=%d net=$%.2f",
            DAILY_STATS["trades"], DAILY_STATS["wins"],
            DAILY_STATS["losses"], DAILY_STATS["net"],
        )

    plan["pnl_logged"] = True
    plan["status"] = "CLOSED"


def reconcile_all_plans() -> None:
    """Reconcile all tracked plans (called on every webhook)."""
    for sym in list(plans.keys()):
        try:
            reconcile_plan(sym)
        except Exception as e:
            logger.error("⚠️ Error reconciling %s: %s", sym, e)


# ============================================================
#   FLASK ROUTES
# ============================================================

@app.route("/tv", methods=["POST"])
def tv_webhook():
    # Reconcile any previous trades first
    reconcile_all_plans()

    # Parse JSON from TradingView
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        logger.exception("❌ Failed to parse JSON from TradingView")
        return jsonify({"status": "error", "message": "invalid json"}), 400

    logger.info("RAW PAYLOAD: %s", payload)

    try:
        plan_data = validate_plan_payload(payload)
    except ValueError as ve:
        logger.error("⚠️ Invalid PLAN payload skipped: %s", ve)
        return jsonify({"status": "error", "message": str(ve)}), 400

    symbol = plan_data["ticker"]
    qty = plan_data["qty"]
    entry_price = plan_data["entry_price"]
    stop_price = plan_data["stop_price"]
    target_price = plan_data["target_price"]
    source = plan_data["source"]

    # If there is already a live plan for this ticker, skip to avoid overlapping brackets.
    existing = plans.get(symbol)
    if existing and existing.get("status") not in ("CLOSED", "CANCELLED", "ERROR"):
        logger.warning(
            "⏭️ PLAN skipped for %s; existing plan status=%s",
            symbol, existing.get("status")
        )
        return jsonify({"status": "skipped", "reason": "plan_exists"}), 200

    plans[symbol] = {
        "ticker": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "source": source,
        "status": "NEW",
        "parent_order_id": None,
        "limit_price": None,
        "created_at": datetime.utcnow().isoformat(),
        "pnl_logged": False,
    }

    logger.info(
        "🧠 New PLAN for %s: qty=%d entry=%.4f stop=%.4f target=%.4f src=%s",
        symbol, qty, entry_price, stop_price, target_price, source,
    )

    parent_id = place_bracket_limit_buy(plans[symbol])
    if not parent_id:
        return jsonify({"status": "error", "message": "order_submit_failed"}), 500

    # Start 1-minute expiry timer for the parent BUY leg
    start_buy_expiry_timer(symbol, parent_id)

    return jsonify({
        "status": "ok",
        "action": "PLAN",
        "ticker": symbol,
        "qty": qty,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
    }), 200


@app.route("/", methods=["GET"])
def healthcheck():
    """Simple health endpoint."""
    return jsonify({"status": "alive", "tracked_symbols": list(plans.keys())})


# ============================================================
#   ENTRY POINT
# ============================================================

if __name__ == "__main__":
    # Railway uses PORT; default to 8080 locally
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)



























































































