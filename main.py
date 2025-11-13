import json
import os
import logging
from datetime import datetime
from typing import Dict, Any

from flask import Flask, request, jsonify

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# -----------------------------
# Config
# -----------------------------

# These MUST match your Railway variables exactly:
API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
ALPACA_BASE_URL = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

# TradingView webhook secret (matches your TV alert)
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_SECRET", "Chrisbot15")

if not API_KEY or not SECRET_KEY:
    raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set")

# Alpaca clients
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True, base_url=ALPACA_BASE_URL)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# In-memory trade tracking (per ticker)
open_trades: Dict[str, Dict[str, Any]] = {}

# Behaviour / buffers
# Option 2:
#   - Above $1  -> +$0.05
#   - Below $1  -> +$0.005
BUY_STEP_ABOVE_1 = 0.05
BUY_STEP_BELOW_1 = 0.005

SELL_UNDER_BID_BUFFER = 0.01   # place sell slightly under current bid to force fill
MAX_SELL_RETRIES = 1           # after a 403 error, stop retrying for that ticker

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

def _within_reason(ref_price: float, quote_price: float, max_diff_pct: float = 20.0) -> bool:
    """
    Returns True if quote_price is within max_diff_pct of ref_price.
    Used so we don't trust totally insane quotes (like 0.46 vs 0.348).
    """
    if ref_price <= 0 or quote_price <= 0:
        return False
    diff_pct = abs(quote_price - ref_price) / ref_price * 100.0
    return diff_pct <= max_diff_pct


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


def place_limit_buy(symbol: str, qty: int, signal_close: float, source: str):
    """
    Limit BUY a new position using a slightly aggressive limit.
    Option 2 buffer: +0.05 (>= $1) or +0.005 (< $1).
    """
    latest = get_latest_quote(symbol)
    ref_price = signal_close

    if latest:
        # Prefer ask if it's sane, otherwise fall back to signal_close
        try:
            ask = float(latest.ask_price)
            if ask > 0 and _within_reason(signal_close, ask, max_diff_pct=20.0):
                ref_price = max(signal_close, ask)
        except Exception:
            pass

    step = BUY_STEP_ABOVE_1 if ref_price >= 1.0 else BUY_STEP_BELOW_1
    limit_price = round(ref_price + step, 4)

    order_req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,  # we manage "give it a minute" at strategy level
        limit_price=limit_price,
    )

    order = trading_client.submit_order(order_req)
    logger.info("✅ BUY placed %s x%d @ %.4f src=%s", symbol, qty, limit_price, source)

    # Track this trade in memory
    open_trades[symbol] = {
        "symbol": symbol,
        "qty": qty,
        "entry_price": float(signal_close),     # signal close
        "entry_limit": float(limit_price),      # actual limit
        "entry_time": datetime.utcnow().isoformat(),
        "source": source,
        "status": "OPEN",
        "sell_retries": 0,
    }
    return order


def place_limit_sell(symbol: str, qty: int, signal_close: float, source: str):
    """
    Limit SELL existing position. Uses an aggressive limit under bid to avoid expiry.
    Uses live Alpaca position qty to avoid 403 "insufficient qty" + wash trade issues.
    """
    # 1) Check live position on Alpaca to avoid 403 "insufficient qty"
    try:
        position = trading_client.get_open_position(symbol)
        pos_qty = float(position.qty)
        if pos_qty <= 0:
            logger.warning("⚠️ No live qty for %s, skipping SELL", symbol)
            return None
        # Use live qty to be safe
        qty = int(pos_qty)
    except Exception as e:
        logger.warning("⚠️ Could not fetch open position for %s, skipping SELL: %s", symbol, e)
        return None

    # 2) Build aggressive limit under current bid / signal_close
    latest = get_latest_quote(symbol)
    ref_sell = signal_close

    if latest:
        try:
            bid = float(latest.bid_price)
            if bid > 0 and _within_reason(signal_close, bid, max_diff_pct=20.0):
                # Go slightly UNDER bid to encourage instant fill
                ref_sell = min(signal_close, bid - SELL_UNDER_BID_BUFFER)
        except Exception:
            pass

    limit_price = round(ref_sell, 4)
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
    """
    mem = open_trades.get(symbol)
    if not mem:
        return

    # Check if still in position
    try:
        position = trading_client.get_open_position(symbol)
        if float(position.qty) > 0:
            return  # still open
    except Exception:
        # No open position -> flat
        pass

    if mem.get("status") in ("CLOSED", "PNL_LOGGED"):
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

    mem["status"] = "PNL_LOGGED"
    # Optional: clean up memory if you want:
    # del open_trades[symbol]


def validate_webhook_payload(payload: Dict[str, Any]):
    """
    Validates and extracts required fields from TradingView payload.
    Tries to be forgiving on SELL (can infer ticker if exactly one trade is open).
    """
    secret = payload.get("secret")
    if secret != WEBHOOK_TOKEN:
        raise ValueError("Invalid secret")

    action = payload.get("action")
    if action not in ("BUY", "SELL"):
        raise ValueError("Invalid or missing action")

    ticker = payload.get("ticker")

    # If SELL has no ticker but exactly one open trade, infer it
    if not ticker:
        if action == "SELL" and len(open_trades) == 1:
            ticker = list(open_trades.keys())[0]
            logger.warning("⚠️ SELL webhook missing ticker; inferred %s from open_trades", ticker)
        else:
            raise ValueError("Missing ticker")

    ticker = str(ticker).upper()

    # Quantity:
    qty_raw = payload.get("quantity", 0)
    try:
        qty = int(qty_raw)
    except Exception:
        qty = 0

    if action == "BUY":
        if qty <= 0:
            raise ValueError("Missing or invalid quantity for BUY")
    else:
        # SELL: we ignore this qty and use live Alpaca position, so we don't hard-fail
        if qty <= 0:
            qty = 1  # dummy, not actually used

    # signal_close
    signal_close_raw = payload.get("signal_close", 0)
    try:
        signal_close = float(signal_close_raw)
    except Exception:
        signal_close = 0.0

    if signal_close <= 0:
        raise ValueError("Missing or invalid signal_close")

    source = payload.get("source", "TV")

    return action, ticker, qty, signal_close, source


# -----------------------------
# Flask route
# -----------------------------

@app.route("/tv", methods=["POST"])
def tv_webhook():
    # Opportunistic PNL logging on every webhook
    for sym in list(open_trades.keys()):
        maybe_log_pnl(sym)

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
        # Allow multiple tickers, but only one trade per ticker
        if ticker in open_trades and open_trades[ticker].get("status") in ("OPEN", "EXITING"):
            logger.warning(
                "⏭️ BUY skipped for %s, already in trade with status %s",
                ticker, open_trades[ticker].get("status")
            )
            return jsonify({"status": "skipped", "reason": "already_in_trade"}), 200

        logger.info("Parsed BUY %s close=%.4f src=%s", ticker, signal_close, source)
        try:
            place_limit_buy(ticker, qty, signal_close, source)
        except Exception:
            logger.exception("❌ Exception while placing BUY for %s", ticker)
            return jsonify({"status": "error", "message": "buy_failed"}), 500

        return jsonify({"status": "ok", "action": "BUY", "ticker": ticker}), 200

    # ----- SELL -----
    if action == "SELL":
        logger.info("Parsed SELL %s close=%.4f src=%s", ticker, signal_close, source)

        if ticker not in open_trades:
            logger.warning("⏭️ SELL skipped — %s not in memory", ticker)
            # Try to close any stray Alpaca position just in case
            try:
                trading_client.close_position(ticker)
                logger.info("Forced close_position on Alpaca for stray %s", ticker)
            except Exception:
                pass
            return jsonify({"status": "skipped", "reason": "no_memory"}), 200

        try:
            place_limit_sell(ticker, open_trades[ticker]["qty"], signal_close, source)
            # PNL will be logged when position actually flat (next webhook)
        except Exception:
            logger.exception("❌ Exception while placing SELL for %s", ticker)
            return jsonify({"status": "error", "message": "sell_failed"}), 500

        return jsonify({"status": "ok", "action": "SELL", "ticker": ticker}), 200

    # Fallback (shouldn’t happen)
    return jsonify({"status": "error", "message": "unreachable"}), 400


if __name__ == "__main__":
    # Railway uses PORT, default 8080 for local
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

























































































