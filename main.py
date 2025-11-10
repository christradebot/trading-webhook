import os
import time
import json
import logging
from datetime import datetime, timedelta, time as dt_time
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ──────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
SECRET_WEBHOOK = os.getenv("WEBHOOK_SECRET", "chrisbot1501")

# Validate credentials before creating client
if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

# Create Alpaca client
trading = TradingClient(API_KEY, SECRET_KEY, paper=("paper" in BASE_URL))

# Flask app
app = Flask(__name__)

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ──────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ──────────────────────────────────────────────────────────────
pending_orders = {}
loss_count = {}
MAX_LOSSES = 2               # Max losses per ticker per day
COOLDOWN_MINUTES = 3         # Minimum minutes between trades per ticker
last_trade_time = {}

# ──────────────────────────────────────────────────────────────
# UTILITY FUNCTIONS
# ──────────────────────────────────────────────────────────────
def get_live_price(symbol: str) -> float:
    """Fetch live bid/ask midpoint from Alpaca."""
    try:
        quote = trading.get_latest_quote(symbol)
        return (quote.bid_price + quote.ask_price) / 2 if quote else 0
    except Exception as e:
        logging.error(f"Failed to fetch live price for {symbol}: {e}")
        return 0

def place_limit_order(symbol, qty, side, limit_price):
    """Place a limit order with safety logging."""
    try:
        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=round(limit_price, 4),
            time_in_force=TimeInForce.DAY
        )
        trading.submit_order(order)
        logging.info(f"✅ {side.upper()} submitted {symbol} x{qty} @ {limit_price}")
        return True
    except Exception as e:
        logging.error(f"❌ Failed to {side} {symbol}: {e}")
        return False

def close_position(symbol):
    """Force close any open position for a symbol."""
    try:
        trading.close_position(symbol, ClosePositionRequest(time_in_force=TimeInForce.DAY))
        logging.info(f"🛑 Closed {symbol} position at market (force).")
    except Exception as e:
        logging.error(f"❌ Close position failed {symbol}: {e}")

# ──────────────────────────────────────────────────────────────
# AUTO-CLOSE ALL AT 19:59
# ──────────────────────────────────────────────────────────────
def should_force_close():
    now = datetime.utcnow().time()
    return dt_time(8, 59) <= now <= dt_time(9, 0)  # 19:59 AEST ≈ 8:59 UTC

def auto_close_positions():
    try:
        positions = trading.get_all_positions()
        for p in positions:
            close_position(p.symbol)
        logging.info("🔒 All open positions closed for EOD (19:59 rule).")
    except Exception as e:
        logging.error(f"Auto-close failed: {e}")

# ──────────────────────────────────────────────────────────────
# FLASK ROUTES
# ──────────────────────────────────────────────────────────────
@app.route("/tv", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    logging.info(f"📩 Webhook received: {data}")

    if not data or data.get("secret") != SECRET_WEBHOOK:
        return jsonify({"error": "unauthorized"}), 403

    action = data.get("action")
    symbol = data.get("ticker")
    qty = int(data.get("quantity", 100))
    signal_close = float(data.get("signal_close", 0))
    source = data.get("source", "UNKNOWN")

    # Safety checks
    if not symbol or not action:
        return jsonify({"error": "missing fields"}), 400

    now = datetime.utcnow()
    if should_force_close():
        auto_close_positions()
        return jsonify({"ok": True, "note": "force-close"}), 200

    # Cooldown check
    last_time = last_trade_time.get(symbol)
    if last_time and (now - last_time).seconds < COOLDOWN_MINUTES * 60:
        return jsonify({"ok": False, "note": "cooldown active"}), 200

    # Loss count check
    if loss_count.get(symbol, 0) >= MAX_LOSSES:
        logging.warning(f"⚠️ Max losses reached for {symbol}")
        return jsonify({"ok": False, "note": "max losses reached"}), 200

    # ────────────── BUY ──────────────
    if action.upper() == "BUY":
        live_price = get_live_price(symbol)
        # dynamic buffer
        if live_price >= 1:
            limit_price = live_price + 0.03
        else:
            limit_price = live_price + 0.003
        success = place_limit_order(symbol, qty, OrderSide.BUY, limit_price)
        if success:
            pending_orders[symbol] = {"side": "BUY", "entry": limit_price, "source": source}
            last_trade_time[symbol] = now
        return jsonify({"ok": success, "limit": limit_price}), 200

    # ────────────── SELL ──────────────
    elif action.upper() == "SELL":
        live_price = get_live_price(symbol)
        target_price = signal_close if abs(signal_close - live_price) / live_price < 0.01 else live_price
        success = place_limit_order(symbol, qty, OrderSide.SELL, target_price)
        if not success:
            close_position(symbol)
        if symbol in loss_count:
            loss_count[symbol] += 1
        else:
            loss_count[symbol] = 1
        return jsonify({"ok": success, "limit": target_price}), 200

    else:
        return jsonify({"error": "unknown action"}), 400

# ──────────────────────────────────────────────────────────────
# RUN APP
# ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    logging.info("🚀 Starting ChrisBot on port %s", port)
    app.run(host="0.0.0.0", port=port)





























































