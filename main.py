import os
import json
import time
import threading
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from flask import Flask, request, jsonify

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ============================================================
# CONFIG
# ============================================================

API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
APCA_API_BASE_URL = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set")

# Alpaca client chooses base URL from env; we only tell it paper/live
is_paper = "paper" in APCA_API_BASE_URL.lower()
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=is_paper)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# Aggressive limit buffers (your choice: always +0.01 / -0.01)
BUY_BUFFER = 0.01
SELL_BUFFER = 0.01

# Entry order timeout (seconds)
ENTRY_TIMEOUT_SEC = 60

# Trailing stop %
DEFAULT_TRAIL_PCT = 15.0

# Flask app
app = Flask(__name__)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("level-bot")

# ============================================================
# STATE
# ============================================================

class LevelTrade:
    def __init__(
        self,
        symbol: str,
        qty: int,
        entry: float,
        stop: float,
        target: float,
        trail_pct: float = DEFAULT_TRAIL_PCT,
        source: str = "TV",
    ):
        self.symbol = symbol
        self.qty = qty
        self.entry = float(entry)
        self.stop = float(stop)
        self.target = float(target)
        self.trail_pct = float(trail_pct)
        self.source = source

        self.status = "PLANNED" # PLANNED -> ENTERED -> EXITING_* -> CLOSED
        self.entry_order_id: Optional[str] = None
        self.exit_order_id: Optional[str] = None

        self.entry_submitted_at: Optional[float] = None
        self.entry_fill_price: Optional[float] = None

        # Trailing
        self.high_water: Optional[float] = None
        self.trail_stop_price: Optional[float] = None

        # PnL
        self.exit_price: Optional[float] = None

    def __repr__(self):
        return (
            f"<LevelTrade {self.symbol} qty={self.qty} "
            f"entry={self.entry} stop={self.stop} target={self.target} "
            f"status={self.status}>"
        )


# Per-ticker trades
trades: Dict[str, LevelTrade] = {}

# Simple daily PnL aggregation
daily_stats: Dict[str, Dict[str, float]] = {}


# ============================================================
# HELPERS: DATA + PRICING
# ============================================================

def get_latest_quote(symbol: str):
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        resp = data_client.get_stock_latest_quote(req)
        quote = resp[symbol]
        return quote
    except Exception as e:
        logger.error("❌ get_latest_quote failed for %s: %s", symbol, e)
        return None


def get_last_price(symbol: str) -> Optional[float]:
    """
    Use mid of bid/ask if available; otherwise fall back to whichever exists.
    """
    q = get_latest_quote(symbol)
    if not q:
        return None
    bid = float(q.bid_price or 0) if q.bid_price is not None else 0.0
    ask = float(q.ask_price or 0) if q.ask_price is not None else 0.0

    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    if ask > 0:
        return ask
    if bid > 0:
        return bid
    return None


def today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def update_daily_pnl(symbol: str, trade: LevelTrade):
    if trade.entry_fill_price is None or trade.exit_price is None:
        return

    entry = float(trade.entry_fill_price)
    exit_price = float(trade.exit_price)
    qty = int(trade.qty)
    pnl = (exit_price - entry) * qty
    pnl_pct = (exit_price / entry - 1.0) * 100.0

    day = today_key()
    stats = daily_stats.setdefault(day, {"trades": 0, "wins": 0, "losses": 0, "net": 0.0})
    stats["trades"] += 1
    stats["net"] += pnl
    if pnl >= 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1

    logger.info(
        "📊 PNL %s: qty=%d entry=%.4f exit=%.4f -> $%.2f (%.2f%%)",
        symbol, qty, entry, exit_price, pnl, pnl_pct,
    )
    logger.info(
        "📈 DAILY SUMMARY %s: trades=%d wins=%d losses=%d net=$%.2f",
        day, stats["trades"], stats["wins"], stats["losses"], stats["net"],
    )


# ============================================================
# HELPERS: ORDERS
# ============================================================

def place_entry_limit(trade: LevelTrade):
    """
    Place a BUY limit only when alert triggers; aggressive +0.01 above ask.
    """
    symbol = trade.symbol
    qty = trade.qty

    quote = get_latest_quote(symbol)
    if quote and quote.ask_price and quote.ask_price > 0:
        ref = float(quote.ask_price)
        logger.info(
            "ℹ️ Entry %s: using live ask %.4f (requested entry %.4f)",
            symbol, ref, trade.entry,
        )
    else:
        ref = float(trade.entry)
        logger.info(
            "ℹ️ Entry %s: NO live ask, falling back to requested entry %.4f",
            symbol, ref,
        )

    limit_price = round(ref + BUY_BUFFER, 4)
    logger.info(
        "🟢 Placing ENTRY BUY for %s: qty=%d limit=%.4f (+%.2f buffer)",
        symbol, qty, limit_price, BUY_BUFFER,
    )

    order_req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )
    order = trading_client.submit_order(order_req)
    trade.entry_order_id = order.id
    trade.entry_submitted_at = time.time()
    trade.status = "PLANNED"

    logger.info("✅ ENTRY order submitted: %s id=%s", symbol, order.id)


def place_exit_limit(symbol: str, qty: int, reason: str) -> Optional[str]:
    """
    Place a SELL limit with aggressive -0.01 under bid.
    """
    quote = get_latest_quote(symbol)
    if quote and quote.bid_price and quote.bid_price > 0:
        ref = float(quote.bid_price)
        logger.info(
            "ℹ️ Exit %s: using live bid %.4f (reason=%s)",
            symbol, ref, reason,
        )
    else:
        last = get_last_price(symbol)
        if last:
            ref = last
            logger.info(
                "ℹ️ Exit %s: NO bid, using last price %.4f (reason=%s)",
                symbol, ref, reason,
            )
        else:
            logger.error("❌ Exit %s: no bid/last price, cannot place sell", symbol)
            return None

    limit_price = round(ref - SELL_BUFFER, 4)
    if limit_price <= 0:
        logger.error("❌ Exit %s: invalid limit %.4f, skip sell", symbol, limit_price)
        return None

    logger.info(
        "🔴 Placing EXIT SELL for %s qty=%d limit=%.4f (-%.2f buffer) reason=%s",
        symbol, qty, limit_price, SELL_BUFFER, reason,
    )

    order_req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )
    order = trading_client.submit_order(order_req)
    logger.info("✅ EXIT order submitted: %s id=%s", symbol, order.id)
    return order.id


def fetch_position_qty(symbol: str) -> float:
    try:
        pos = trading_client.get_open_position(symbol)
        return float(pos.qty)
    except Exception:
        return 0.0


def fetch_order_status(order_id: str) -> Optional[str]:
    try:
        order = trading_client.get_order_by_id(order_id)
        return order.status
    except Exception as e:
        logger.error("❌ fetch_order_status failed for %s: %s", order_id, e)
        return None


def fetch_order_fill_price(order_id: str) -> Optional[float]:
    try:
        order = trading_client.get_order_by_id(order_id)
        if order.filled_avg_price:
            return float(order.filled_avg_price)
        return None
    except Exception as e:
        logger.error("❌ fetch_order_fill_price failed for %s: %s", order_id, e)
        return None


# ============================================================
# CORE LOGIC: MONITOR LOOP
# ============================================================

def monitor_trades_loop():
    """
    Background loop:
      - watches for ENTRY fills / timeouts
      - tracks price for each ENTERED trade
      - triggers exits for stop, target, trailing
      - logs PnL on close
    """
    while True:
        try:
            for symbol, trade in list(trades.items()):
                # ENTRY handling
                if trade.status == "PLANNED" and trade.entry_order_id:
                    status = fetch_order_status(trade.entry_order_id)
                    now = time.time()
                    age = now - (trade.entry_submitted_at or now)

                    logger.debug(
                        "👀 Monitor %s: entry status=%s age=%.1fs",
                        symbol, status, age,
                    )

                    if status == "filled":
                        fill_price = fetch_order_fill_price(trade.entry_order_id)
                        if fill_price:
                            trade.entry_fill_price = fill_price
                        else:
                            # fallback: use current last price
                            fill_price = get_last_price(symbol) or trade.entry
                            trade.entry_fill_price = fill_price

                        trade.status = "ENTERED"
                        trade.high_water = trade.entry_fill_price
                        trade.trail_stop_price = trade.entry_fill_price * (1 - trade.trail_pct / 100.0)

                        logger.info(
                            "✅ ENTRY FILLED %s qty=%d at %.4f; trail_stop=%.4f (%.1f%%)",
                            symbol, trade.qty, trade.entry_fill_price,
                            trade.trail_stop_price, trade.trail_pct,
                        )

                    elif age > ENTRY_TIMEOUT_SEC and status not in ("filled", "canceled", "expired"):
                        logger.warning(
                            "⏱ ENTRY TIMEOUT %s after %.1fs, cancelling order",
                            symbol, age,
                        )
                        try:
                            trading_client.cancel_order_by_id(trade.entry_order_id)
                        except Exception as e:
                            logger.error("❌ cancel_order failed %s: %s", symbol, e)
                        trade.status = "CANCELLED"

                # EXIT handling
                if trade.status == "ENTERED":
                    price = get_last_price(symbol)
                    if price is None:
                        logger.debug("👀 %s: no price, skip this cycle", symbol)
                        continue

                    # Ensure we have live qty
                    live_qty = fetch_position_qty(symbol)
                    if live_qty <= 0:
                        logger.warning(
                            "⚠️ %s: ENTERED in memory but no live position, marking CLOSED",
                            symbol,
                        )
                        trade.status = "CLOSED"
                        continue

                    # Update high water & trail stop
                    if trade.high_water is None or price > trade.high_water:
                        old_hw = trade.high_water
                        trade.high_water = price
                        new_trail = trade.high_water * (1 - trade.trail_pct / 100.0)
                        if trade.trail_stop_price is None or new_trail > trade.trail_stop_price:
                            logger.info(
                                "🏔 %s: new high_water %.4f (was %.4f), trail_stop -> %.4f",
                                symbol, trade.high_water, old_hw, new_trail,
                            )
                            trade.trail_stop_price = new_trail

                    # Decide exit reason
                    reason = None

                    # 1) Hard stop
                    if price <= trade.stop:
                        reason = "STOP"
                        logger.info(
                            "🛑 %s: price %.4f <= STOP %.4f -> STOP EXIT",
                            symbol, price, trade.stop,
                        )

                    # 2) Target
                    elif price >= trade.target:
                        reason = "TARGET"
                        logger.info(
                            "🎯 %s: price %.4f >= TARGET %.4f -> TARGET EXIT",
                            symbol, price, trade.target,
                        )

                    # 3) Trailing
                    elif trade.trail_stop_price is not None and price <= trade.trail_stop_price:
                        reason = "TRAIL"
                        logger.info(
                            "🔁 %s: price %.4f <= TRAIL_STOP %.4f -> TRAIL EXIT",
                            symbol, price, trade.trail_stop_price,
                        )

                    if reason:
                        exit_id = place_exit_limit(symbol, int(live_qty), reason)
                        if exit_id:
                            trade.exit_order_id = exit_id
                            trade.status = f"EXITING_{reason}"

                # Check exit fills
                if trade.status.startswith("EXITING") and trade.exit_order_id:
                    status = fetch_order_status(trade.exit_order_id)
                    logger.debug(
                        "👀 Monitor %s: exit status=%s", symbol, status,
                    )

                    if status == "filled":
                        fill_price = fetch_order_fill_price(trade.exit_order_id)
                        if fill_price:
                            trade.exit_price = fill_price
                        else:
                            trade.exit_price = get_last_price(symbol) or trade.target

                        trade.status = "CLOSED"
                        logger.info(
                            "✅ EXIT FILLED %s at %.4f (status %s)",
                            symbol, trade.exit_price, status,
                        )
                        update_daily_pnl(symbol, trade)

        except Exception as loop_err:
            logger.error("❌ Exception in monitor loop: %s", loop_err)

        time.sleep(5) # adjust if needed


# Start background monitor thread
monitor_thread = threading.Thread(target=monitor_trades_loop, daemon=True)
monitor_thread.start()


# ============================================================
# VALIDATION
# ============================================================

def validate_secret(payload: Dict[str, Any]):
    secret = payload.get("secret")
    if secret != WEBHOOK_SECRET:
        raise ValueError("Invalid secret")


def parse_level_plan(payload: Dict[str, Any]) -> LevelTrade:
    """
    Parse level-based plan from payload.
    Accepts actions: PLAN / LEVEL / BUY (for compatibility).
    Required fields: ticker, quantity, entry, stop, target.
    Optional: trail_pct, source.
    """
    validate_secret(payload)

    action = payload.get("action", "").upper()
    if action not in ("PLAN", "LEVEL", "BUY"):
        raise ValueError(f"Unsupported action for level plan: {action}")

    ticker = payload.get("ticker")
    if not ticker or not isinstance(ticker, str):
        raise ValueError("Missing ticker")

    qty = int(payload.get("quantity", 0))
    if qty <= 0:
        raise ValueError("Missing or invalid quantity")

    try:
        entry = float(payload.get("entry"))
        stop = float(payload.get("stop"))
        target = float(payload.get("target"))
    except Exception:
        raise ValueError("Missing or invalid entry/stop/target")

    if entry <= 0 or stop <= 0 or target <= 0:
        raise ValueError("entry/stop/target must be > 0")

    trail_pct = float(payload.get("trail_pct", DEFAULT_TRAIL_PCT))
    source = payload.get("source", "TV")

    trade = LevelTrade(
        symbol=ticker.upper(),
        qty=qty,
        entry=entry,
        stop=stop,
        target=target,
        trail_pct=trail_pct,
        source=source,
    )

    logger.info(
        "📝 New LEVEL PLAN %s qty=%d entry=%.4f stop=%.4f target=%.4f trail=%.1f%% src=%s",
        trade.symbol, trade.qty, trade.entry, trade.stop, trade.target,
        trade.trail_pct, trade.source,
    )
    return trade


# ============================================================
# FLASK ROUTE
# ============================================================

@app.route("/tv", methods=["POST"])
def tv_webhook():
    # Parse JSON safely
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception as e:
        logger.exception("❌ Failed to parse JSON from TradingView: %s", e)
        return jsonify({"status": "error", "message": "invalid json"}), 400

    logger.info("RAW PAYLOAD: %s", payload)

    if not isinstance(payload, dict):
        return jsonify({"status": "error", "message": "invalid payload type"}), 400

    # Handle CANCEL requests explicitly
    action = str(payload.get("action", "")).upper()

    # -------------------------
    # CANCEL by ticker or all
    # -------------------------
    if action in ("CANCEL", "CANCEL_PLAN"):
        try:
            validate_secret(payload)
        except ValueError as ve:
            logger.error("⚠️ Cancel: invalid secret: %s", ve)
            return jsonify({"status": "error", "message": str(ve)}), 400

        ticker = payload.get("ticker")
        if ticker:
            sym = ticker.upper()
            if sym in trades:
                logger.info("🧹 Cancel plan for %s", sym)
                trades.pop(sym, None)
                return jsonify({"status": "ok", "message": f"plan_cancelled_{sym}"}), 200
            else:
                return jsonify({"status": "skipped", "message": "no_plan_for_ticker"}), 200
        else:
            logger.info("🧹 Cancel ALL plans")
            trades.clear()
            return jsonify({"status": "ok", "message": "all_plans_cleared"}), 200

    # -------------------------
    # LEVEL PLAN / ENTRY
    # -------------------------
    try:
        trade = parse_level_plan(payload)
    except ValueError as ve:
        logger.error("⚠️ Invalid level payload: %s", ve)
        return jsonify({"status": "error", "message": str(ve)}), 400

    symbol = trade.symbol

    # If we already have a plan for this symbol, skip to avoid chaos
    existing = trades.get(symbol)
    if existing and existing.status not in ("CLOSED", "CANCELLED"):
        logger.warning(
            "⏭️ PLAN skipped for %s, existing trade status=%s",
            symbol, existing.status,
        )
        return jsonify({"status": "skipped", "reason": "existing_trade"}), 200

    # Register plan and place entry immediately (alert fires when level is touched)
    trades[symbol] = trade

    try:
        place_entry_limit(trade)
    except Exception as e:
        logger.exception("❌ Exception while placing ENTRY for %s: %s", symbol, e)
        return jsonify({"status": "error", "message": "entry_failed"}), 500

    return jsonify({"status": "ok", "symbol": symbol, "mode": "level_plan"}), 200


# ============================================================
# ENTRYPOINT
# ============================================================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    logger.info("Starting Flask app on 0.0.0.0:%d", port)
    app.run(host="0.0.0.0", port=port)





























































































