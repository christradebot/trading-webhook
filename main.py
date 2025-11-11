import os
import time
import json
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
BASE_URL = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "CHRISBOT150")

if not all([API_KEY, SECRET_KEY]):
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ─────────────────────────────────────────────
# GLOBALS
# ─────────────────────────────────────────────
open_positions = {}
max_losses_per_ticker = {}
MAX_LOSSES = 2

# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────
def get_latest_price(symbol):
    """Fetch real-time bid/ask to place realistic limit orders."""
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quote = data_client.get_stock_latest_quote(req)
        bid = quote[symbol].bid_price or 0
        ask = quote[symbol].ask_price or 0
        return (bid + ask) / 2 if bid and ask else 0
    except Exception as e:
        logging.error(f"⚠️ Price fetch failed for {symbol}: {e}")
        return 0

def limit_buffer(close_price):
    """Dynamic buffer: +0.03 for >$1 stocks, +0.003 for <$1."""
    return 0.03 if close_price >= 1 else 0.003

def place_limit_order(symbol, qty, side, ref_price):
    """Place a limit order with small offset for real fills."""
    price = ref_price + limit_buffer(ref_price) if side == "buy" else ref_price - limit_buffer(ref_price)
    limit_price = round(price, 4)

    try:
        order = trading_client.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                type="limit",
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price
            )
        )
        logging.info(f"✅ {side.upper()} placed for {symbol} x{qty} @ {limit_price}")
        return order
    except Exception as e:
        logging.error(f"❌ Order failed for {symbol}: {e}")
        return None

def close_position(symbol, last_close):
    """Close open position aggressively if needed."""
    for attempt in range(10):
        try:
            live_price = get_latest_price(symbol)
            if not live_price:
                live_price = last_close

            limit_price = round(min(live_price, last_close) * 0.995, 4)
            order = trading_client.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=open_positions.get(symbol, 0),
                    side=OrderSide.SELL,
                    type="limit",
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price
                )
            )
            logging.info(f"🚨 SELL order placed {symbol} @ {limit_price} (attempt {attempt+1})")
            time.sleep(3)
            return
        except Exception as e:
            logging.warning(f"Retrying close {symbol}: {e}")
            time.sleep(2)

    logging.error(f"❌ Failed to close {symbol} after 10 tries")

# ─────────────────────────────────────────────
# FLASK ENDPOINT
# ─────────────────────────────────────────────
@app.route("/tv", methods=["POST"])
def tv():
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "415 Unsupported Media Type - could not parse JSON"}), 415

    if not payload:
        return jsonify({"error": "Empty payload"}), 400

    if payload.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Invalid secret"}), 403

    action = payload.get("action", "").upper()
    symbol = payload.get("ticker", "").upper()
    qty = int(payload.get("quantity", 0))
    signal_close = payload.get("signal_close")

    try:
        target_close = float(signal_close)
    except Exception:
        target_close = 0.0

    logging.info(f"✅ Parsed: {action} {symbol} x{qty} close={target_close}")

    # Limit losses safeguard
    if max_losses_per_ticker.get(symbol, 0) >= MAX_LOSSES:
        return jsonify({"error": f"❌ {symbol}: max loss limit reached"}), 403

    # ────────── BUY
    if action == "BUY":
        live_price = get_latest_price(symbol)
        ref_price = target_close or live_price
        if not ref_price:
            return jsonify({"error": "No valid price"}), 400
        place_limit_order(symbol, qty, "buy", ref_price)
        open_positions[symbol] = qty

    # ────────── SELL
    elif action == "SELL":
        if symbol in open_positions:
            close_position(symbol, target_close)
            del open_positions[symbol]
            logging.info(f"✅ {symbol} position fully closed")

    # ────────── TEST
    elif action == "TEST":
        return jsonify({"ok": True, "symbol": symbol})

    return jsonify({"ok": True, "symbol": symbol, "action": action})

# ─────────────────────────────────────────────
# AUTO-CLOSE POSITIONS AT 19:59
# ─────────────────────────────────────────────
@app.before_request
def auto_close_at_end_of_day():
    now = datetime.utcnow()
    if now.hour == 23 and now.minute >= 59:  # 7:59pm ET
        for symbol in list(open_positions.keys()):
            close_position(symbol, get_latest_price(symbol))
            del open_positions[symbol]
            logging.info(f"🕖 Auto-closed {symbol} at EOD")

# ─────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)
















































































