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

API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

# TradingView webhook secret (can override via env)
WEBHOOK_TOKEN = os.environ.get("TV_WEBHOOK_SECRET", "CHRISBOI1501")

if not API_KEY or not SECRET_KEY:
    raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY not set")

# Alpaca clients
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# In-memory trade tracking (per ticker)
open_trades: Dict[str, Dict[str, Any]] = {}

# Behaviour / buffers
ENTRY_LIMIT_BUFFER = 0.02      # add to buy price to increase chance of fill
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
    """
    latest = get_latest_quote(symbol)
    ref_price = signal_close

    if latest:
        # If we have an ask, use max(signal_close, ask)
        try:
            ask = float(latest.ask_price)
            if ask > 0:
                ref_price = max(signal_close, ask)
        except Exception:
            pass

    limit_price = round(ref_price + ENTRY_LIMIT_BUFFER, 4)

    order_req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
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
            if bid > 0:
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
    # Optional: clean up memory if you want
    # del open_trades[symbol]


def validate_webhook_payload(payload: Dict[str, Any]):
    """
    Validates and extracts required fields from TradingView payload.
    """
    secret = payload.get("secret")
    if secret != WEBHOOK_TOKEN:
        raise ValueError("Invalid secret")

    action = payload.get("action")
    if action not in ("BUY", "SELL"):
        raise ValueError("Invalid or missing action")

    ticker = payload.get("ticker")
    if not ticker or not isinstance(ticker, str):
        raise ValueError("Missing ticker")

    qty = int(payload.get("quantity", 0))
    if qty <= 0:
        raise ValueError("Missing or invalid quantity")

    signal_close = float(payload.get("signal_close", 0))
    if signal_close <= 0:
        raise ValueError("Missing or invalid signal_close")

    source = payload.get("source", "TV")

    return action, ticker.upper(), qty, signal_close, source


# -----------------------------
# Flask route
# -----------------------------

@app.route("/tv", methods=["POST"])
def tv_webhook():
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























































































