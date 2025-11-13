import json
import os
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional

from flask import Flask, request, jsonify

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# -----------------------------
# Config - MATCHES RAILWAY VARS
# -----------------------------

API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
ALPACA_BASE_URL = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set")

# Alpaca clients - NO base_url kwarg (SDK uses env var)
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# In-memory trade tracking (only ONE active trade at a time)
open_trades: Dict[str, Dict[str, Any]] = {}

# Behaviour / buffers
ENTRY_ABSOLUTE_BUFFER = 0.05 # +$0.05 for all stocks (above & below $1)
SELL_UNDER_BID_BUFFER = 0.01 # place sell slightly under current bid
MAX_SELL_RETRIES = 1 # after a 403 error, stop retrying for that ticker
ORDER_EXPIRY_SECONDS = 60 # 1 minute to fill, then cancel

# Simple daily P&L stats (reset when process restarts)
daily_stats = {
    "realized": 0.0,
    "trades": 0,
    "wins": 0,
    "losses": 0,
}

# Flask app
app = Flask(__name__)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("main-bot")


# -----------------------------
# Helpers
# -----------------------------

def _now_utc() -> datetime:
    return datetime.utcnow()


def get_latest_quote(symbol: str):
    """Fetch latest quote (bid/ask) from Alpaca."""
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        resp = data_client.get_stock_latest_quote(req)
        quote = resp[symbol]
        return quote
    except Exception as e:
        logger.error("Failed to fetch latest quote for %s: %s", symbol, e)
        return None


def cancel_expired_buy_orders():
    """
    Cancel BUY limit orders that have been sitting for more than ORDER_EXPIRY_SECONDS.
    Called on every webhook hit (approximation, no background thread).
    """
    now = _now_utc()
    for symbol, mem in list(open_trades.items()):
        if mem.get("status") != "OPEN":
            continue
        order_id = mem.get("entry_order_id")
        entry_time_str = mem.get("entry_time")
        if not order_id or not entry_time_str:
            continue
        try:
            entry_time = datetime.fromisoformat(entry_time_str)
        except Exception:
            continue

        age = (now - entry_time).total_seconds()
        if age > ORDER_EXPIRY_SECONDS:
            try:
                trading_client.cancel_order_by_id(order_id)
                logger.info(
                    "⌛ BUY expired and cancelled for %s (order_id=%s, age=%.1fs)",
                    symbol, order_id, age,
                )
                mem["status"] = "EXPIRED"
            except Exception as e:
                logger.error("Failed to cancel expired BUY for %s: %s", symbol, e)


def place_limit_buy(symbol: str, qty: int, signal_close: float, source: str):
    """
    Limit BUY a new position using a fixed +$0.05 buffer.
    (for both stocks above and below $1 as per Chris)
    """
    latest = get_latest_quote(symbol)
    ref_price = signal_close

    if latest:
        try:
            ask = float(latest.ask_price)
            if ask > 0:
                # Use the higher of signal close and ask
                ref_price = max(signal_close, ask)
        except Exception:
            pass

    # Fixed 5-cent buffer as requested
    limit_price = round(ref_price + ENTRY_ABSOLUTE_BUFFER, 4)

    order_req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )

    order = trading_client.submit_order(order_req)
    order_id = getattr(order, "id", None)

    logger.info("✅ BUY placed %s x%d @ %.4f src=%s", symbol, qty, limit_price, source)

    open_trades.clear() # enforce ONE trade at a time globally
    open_trades[symbol] = {
        "symbol": symbol,
        "qty": qty,
        "entry_price": float(signal_close), # signal close
        "entry_limit": float(limit_price), # actual limit
        "entry_time": _now_utc().isoformat(),
        "entry_order_id": order_id,
        "source": source,
        "status": "OPEN",
        "sell_retries": 0,
    }
    return order


def place_limit_sell(symbol: str, qty_hint: int, signal_close: float, source: str):
    """
    Limit SELL existing position. Uses an aggressive limit under bid to avoid expiry.
    Does NOT trust TradingView for quantity; uses live position from Alpaca.
    """
    # 1) Check live position on Alpaca to avoid 403 "insufficient qty"
    try:
        position = trading_client.get_open_position(symbol)
        pos_qty = float(position.qty)
        if pos_qty <= 0:
            logger.warning("⚠️ No live qty for %s, skipping SELL", symbol)
            return None
        qty = int(pos_qty)
    except Exception as e:
        logger.warning("⚠️ Could not fetch open position for %s, skipping SELL: %s", symbol, e)
        return None

    # 2) Build aggressive limit under current bid / signal_close
    latest = get_latest_quote(symbol)
    ref_sell = signal_close if signal_close > 0 else None

    if latest:
        try:
            bid = float(latest.bid_price)
            if bid > 0:
                # Go slightly UNDER bid to encourage instant fill
                candidate = bid - SELL_UNDER_BID_BUFFER
                ref_sell = candidate if ref_sell is None else min(ref_sell, candidate)
        except Exception:
            pass

    if ref_sell is None:
        # No signal_close and no usable bid -> fallback to entry price if we have it
        mem = open_trades.get(symbol)
        if mem:
            ref_sell = float(mem.get("entry_price", 0.0))

    limit_price = round(ref_sell or 0.0, 4)

    if limit_price <= 0:
        logger.error("❌ Invalid SELL price %.4f for %s, skipping", limit_price, symbol)
        return None

    order_req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )

    mem = open_trades.get(symbol)

    try:
        order = trading_client.submit_order(order_req)
        logger.info("✅ SELL placed %s x%d @ %.4f src=%s", symbol, qty, limit_price, source)

        if mem:
            mem["exit_limit"] = float(limit_price)
            mem["status"] = "EXITING"
        return order

    except Exception as e:
        msg = str(e)
        logger.error("⚠️ SELL submit failed %s: %s", symbol, msg)
        if mem:
            mem["sell_retries"] = mem.get("sell_retries", 0) + 1
            # Stop the infinite spam loop after we hit retry limit
            if mem["sell_retries"] >= MAX_SELL_RETRIES:
                logger.error("🚫 Stopping further SELL attempts for %s after error: %s", symbol, msg)
        return None


def maybe_log_pnl(symbol: str):
    """
    If a trade is flat on Alpaca, log P&L once and mark it as done.
    Also updates daily_stats.
    """
    mem = open_trades.get(symbol)
    if not mem:
        return

    # Check if still in position
    try:
        position = trading_client.get_open_position(symbol)
        if float(position.qty) > 0:
            return # still open
    except Exception:
        # No open position -> flat
        pass

    if mem.get("status") in ("CLOSED", "PNL_LOGGED", "EXPIRED"):
        return

    entry = float(mem.get("entry_price", 0))
    exit_price = float(mem.get("exit_limit", 0))
    qty = int(mem.get("qty", 0))

    if entry > 0 and exit_price > 0 and qty > 0:
        pnl = (exit_price - entry) * qty
        pnl_pct = (exit_price / entry - 1.0) * 100.0
        logger.info(
            "📊 PNL %s: qty=%d entry=%.4f exit=%.4f -> $%.2f (%.2f%%)",
            symbol, qty, entry, exit_price, pnl, pnl_pct,
        )

        # Update daily stats
        daily_stats["realized"] += pnl
        daily_stats["trades"] += 1
        if pnl > 0:
            daily_stats["wins"] += 1
        elif pnl < 0:
            daily_stats["losses"] += 1

        logger.info(
            "📈 DAILY SUMMARY: trades=%d wins=%d losses=%d net=$%.2f",
            daily_stats["trades"],
            daily_stats["wins"],
            daily_stats["losses"],
            daily_stats["realized"],
        )

    mem["status"] = "PNL_LOGGED"


def validate_webhook_payload(payload: Dict[str, Any]):
    """
    Validates and extracts required fields from TradingView payload.

    - BUY: requires action + ticker + quantity + signal_close
    - SELL: requires action only (ticker & qty optional; symbol can be inferred)
    """
    secret = payload.get("secret")
    if secret != WEBHOOK_SECRET:
        raise ValueError("Invalid secret")

    raw_action = payload.get("action") or payload.get("side")
    if not raw_action:
        raise ValueError("Invalid or missing action")
    action = str(raw_action).upper()

    # Ticker can be missing on SELL; we'll infer it from open_trades if needed
    ticker = payload.get("ticker") or payload.get("symbol")
    source = payload.get("source", "TV")

    # BUY specific requirements
    if action == "BUY":
        if not ticker or not isinstance(ticker, str):
            raise ValueError("Missing ticker for BUY")

        qty_val = payload.get("quantity") or payload.get("qty")
        try:
            qty = int(qty_val)
        except (TypeError, ValueError):
            qty = 0
        if qty <= 0:
            raise ValueError("Missing or invalid quantity for BUY")

        sig_val = payload.get("signal_close") or payload.get("price") or payload.get("close")
        try:
            signal_close = float(sig_val)
        except (TypeError, ValueError):
            signal_close = 0.0
        if signal_close <= 0:
            raise ValueError("Missing or invalid signal_close for BUY")

        return action, ticker.upper(), qty, signal_close, source

    # SELL: ticker/qty/signal_close optional
    if action == "SELL":
        qty = 0 # we ignore TV qty & use Alpaca position anyway

        sig_val = payload.get("signal_close") or payload.get("price") or payload.get("close")
        try:
            signal_close = float(sig_val) if sig_val is not None else 0.0
        except (TypeError, ValueError):
            signal_close = 0.0

        return action, (ticker.upper() if isinstance(ticker, str) else None), qty, signal_close, source

    raise ValueError("Unsupported action")


def infer_sell_ticker_if_missing(ticker: Optional[str]) -> Optional[str]:
    """If SELL came without a ticker, infer from the single open trade (one-trade-only rule)."""
    if ticker:
        return ticker

    if len(open_trades) == 1:
        inferred = next(iter(open_trades.keys()))
        logger.info("ℹ️ SELL ticker inferred from single open trade: %s", inferred)
        return inferred

    if len(open_trades) == 0:
        logger.warning("⚠️ SELL with no ticker and no open trades; nothing to close.")
    else:
        logger.warning("⚠️ SELL with no ticker and multiple open trades; cannot infer uniquely.")

    return None


# -----------------------------
# Flask route
# -----------------------------

@app.route("/tv", methods=["POST"])
def tv_webhook():
    # Cancel expired BUY orders (1-minute FOK-style behaviour)
    cancel_expired_buy_orders()

    # Parse JSON
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        logger.exception("❌ Failed to parse JSON from TradingView")
        return jsonify({"status": "error", "message": "invalid json"}), 400

    logger.info("RAW PAYLOAD: %s", payload)

    # Validate
    try:
        action, ticker, qty, signal_close, source = validate_webhook_payload(payload)
    except ValueError as ve:
        # Only 400 here for truly bad payload/secret
        logger.error("⚠️ Invalid payload skipped: %s", ve)
        return jsonify({"status": "error", "message": str(ve)}), 400

    # ----- BUY -----
    if action == "BUY":
        # Enforce ONE active trade at a time across all tickers
        for sym, mem in open_trades.items():
            if mem.get("status") in ("OPEN", "EXITING"):
                logger.warning(
                    "⏭️ BUY skipped for %s; existing trade open on %s with status %s",
                    ticker, sym, mem.get("status"),
                )
                return jsonify({"status": "skipped", "reason": "existing_trade_open"}), 200

        logger.info("Parsed BUY %s close=%.4f src=%s", ticker, signal_close, source)
        try:
            place_limit_buy(ticker, qty, signal_close, source)
        except Exception:
            logger.exception("❌ Exception while placing BUY for %s", ticker)
            return jsonify({"status": "error", "message": "buy_failed"}), 500

        return jsonify({"status": "ok", "action": "BUY", "ticker": ticker}), 200

    # ----- SELL -----
    if action == "SELL":
        # Infer ticker if TradingView didn't send one
        ticker = infer_sell_ticker_if_missing(ticker)

        if not ticker:
            return jsonify({"status": "skipped", "reason": "no_ticker"}), 200

        logger.info("Parsed SELL %s close=%.4f src=%s", ticker, signal_close, source)

        mem = open_trades.get(ticker)
        if not mem:
            logger.warning("⏭️ SELL skipped — %s not in memory; trying direct close_position", ticker)
            # Try to close any stray Alpaca position just in case
            try:
                trading_client.close_position(ticker)
                logger.info("Forced close_position on Alpaca for stray %s", ticker)
            except Exception as e:
                logger.warning("close_position failed for %s: %s", ticker, e)
            return jsonify({"status": "skipped", "reason": "no_memory"}), 200

        try:
            # Use stored qty hint & signal_close (can be 0 -> bid-based)
            place_limit_sell(ticker, mem["qty"], signal_close, source)
            maybe_log_pnl(ticker)
        except Exception:
            logger.exception("❌ Exception while placing SELL for %s", ticker)
            return jsonify({"status": "error", "message": "sell_failed"}), 500

        return jsonify({"status": "ok", "action": "SELL", "ticker": ticker}), 200

    # Fallback (shouldn’t happen)
    return jsonify({"status": "error", "message": "unreachable"}), 400


if __name__ == "__main__":
    # Railway uses PORT, default 8080 for local
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

























































































