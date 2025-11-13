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

# ==========================================================
# CONFIG — MATCHES RAILWAY VARIABLES EXACTLY
# ==========================================================

API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
ALPACA_BASE_URL = os.environ.get("APCA_API_BASE_URL")
WEBHOOK_TOKEN = os.environ.get("WEBHOOK_SECRET")

if not API_KEY or not SECRET_KEY:
    raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set")

# Alpaca clients
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# In-memory trade tracking
open_trades: Dict[str, Dict[str, Any]] = {}

# Buffers & limits
ENTRY_LIMIT_BUFFER = 0.02
SELL_UNDER_BID_BUFFER = 0.01
MAX_SELL_RETRIES = 1

# Flask
app = Flask(__name__)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("main-bot")


# ==========================================================
# HELPER FUNCTIONS
# ==========================================================

def get_latest_quote(symbol: str):
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        resp = data_client.get_stock_latest_quote(req)
        return resp[symbol]
    except Exception as e:
        logger.error("Quote fetch failed for %s: %s", symbol, e)
        return None


def place_limit_buy(symbol: str, qty: int, signal_close: float, source: str):
    latest = get_latest_quote(symbol)
    ref_price = signal_close

    if latest:
        try:
            ask = float(latest.ask_price)
            if ask > 0:
                ref_price = max(signal_close, ask)
        except:
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
    logger.info("BUY placed %s x%d @ %.4f [%s]", symbol, qty, limit_price, source)

    open_trades[symbol] = {
        "symbol": symbol,
        "qty": qty,
        "entry_price": float(signal_close),
        "entry_limit": float(limit_price),
        "entry_time": datetime.utcnow().isoformat(),
        "source": source,
        "status": "OPEN",
        "sell_retries": 0,
    }

    return order


def place_limit_sell(symbol: str, qty: int, signal_close: float, source: str):
    # ensure we have live qty
    try:
        pos = trading_client.get_open_position(symbol)
        qty = int(float(pos.qty))
    except:
        logger.warning("No live qty for %s — skipping sell.", symbol)
        return None

    latest = get_latest_quote(symbol)
    ref_price = signal_close

    if latest:
        try:
            bid = float(latest.bid_price)
            if bid > 0:
                ref_price = min(signal_close, bid - SELL_UNDER_BID_BUFFER)
        except:
            pass

    limit_price = round(ref_price, 4)

    if limit_price <= 0:
        logger.error("Invalid SELL limit for %s", symbol)
        return None

    order_req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )

    try:
        order = trading_client.submit_order(order_req)
        logger.info("SELL placed %s x%d @ %.4f [%s]", symbol, qty, limit_price, source)
        if symbol in open_trades:
            open_trades[symbol]["exit_limit"] = float(limit_price)
            open_trades[symbol]["status"] = "EXITING"
        return order

    except Exception as e:
        logger.error("SELL failed %s: %s", symbol, e)
        if symbol in open_trades:
            open_trades[symbol]["sell_retries"] += 1
        return None


def maybe_log_pnl(symbol: str):
    mem = open_trades.get(symbol)
    if not mem:
        return

    # still in position?
    try:
        pos = trading_client.get_open_position(symbol)
        if float(pos.qty) > 0:
            return
    except:
        pass

    if mem.get("status") == "PNL_LOGGED":
        return

    entry = mem.get("entry_price", 0)
    exitp = mem.get("exit_limit", 0)
    qty = mem.get("qty", 0)

    if entry and exitp:
        pnl = (exitp - entry) * qty
        pct = (exitp / entry - 1) * 100
        logger.info("PNL %s: entry=%.4f exit=%.4f qty=%d -> $%.2f (%.2f%%)",
                    symbol, entry, exitp, qty, pnl, pct)

    mem["status"] = "PNL_LOGGED"


def validate_payload(p):
    if p.get("secret") != WEBHOOK_TOKEN:
        raise ValueError("Invalid secret")

    action = p.get("action")
    if action not in ("BUY", "SELL"):
        raise ValueError("Invalid action")

    ticker = p.get("ticker")
    if not ticker:
        raise ValueError("Missing ticker")

    qty = int(p.get("quantity", 0))
    if qty <= 0:
        raise ValueError("Invalid qty")

    close = float(p.get("signal_close", 0))
    if close <= 0:
        raise ValueError("Invalid signal_close")

    return action, ticker.upper(), qty, close, p.get("source", "TV")


# ==========================================================
# FLASK ROUTE
# ==========================================================

@app.route("/tv", methods=["POST"])
def tv():
    try:
        payload = request.get_json(force=True)
    except:
        return jsonify({"error": "bad json"}), 400

    logger.info("RAW PAYLOAD: %s", payload)

    try:
        action, ticker, qty, close, source = validate_payload(payload)
    except ValueError as e:
        logger.error("Invalid payload: %s", e)
        return jsonify({"error": str(e)}), 400

    # BUY
    if action == "BUY":
        if ticker in open_trades and open_trades[ticker]["status"] in ("OPEN", "EXITING"):
            logger.info("BUY skipped for %s: already open", ticker)
            return jsonify({"status": "skipped"}), 200

        place_limit_buy(ticker, qty, close, source)
        return jsonify({"status": "buy_ok"}), 200

    # SELL
    if action == "SELL":
        if ticker not in open_trades:
            # force close stray
            try:
                trading_client.close_position(ticker)
            except:
                pass
            return jsonify({"status": "skipped_no_memory"}), 200

        place_limit_sell(ticker, open_trades[ticker]["qty"], close, source)
        maybe_log_pnl(ticker)
        return jsonify({"status": "sell_ok"}), 200

    return jsonify({"error": "unreachable"}), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
























































































