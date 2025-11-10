import os
import json
import time
import logging
from datetime import datetime, time as dtime
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ─────────────────────────────────────────────
# Logging setup
# ─────────────────────────────────────────────
logging.basicConfig(
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO
)

app = Flask(__name__)

# ─────────────────────────────────────────────
# Load environment variables
# ─────────────────────────────────────────────
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
PAPER = os.environ.get("ALPACA_PAPER", "true").lower() == "true"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

if not WEBHOOK_SECRET:
    raise ValueError("🚨 WEBHOOK_SECRET missing in Railway Variables.")

logging.info(f"🔐 Loaded webhook secret from environment: '{WEBHOOK_SECRET}'")

# ─────────────────────────────────────────────
# Alpaca client
# ─────────────────────────────────────────────
trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)

# ─────────────────────────────────────────────
# Helper: determine buffer based on price
# ─────────────────────────────────────────────
def get_buffer(price):
    return 0.03 if price > 1 else 0.003

# ─────────────────────────────────────────────
# Webhook endpoint
# ─────────────────────────────────────────────
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    logging.info(f"📩 Webhook received: {data}")

    # ✅ Case-insensitive secret match
    if data.get("secret", "").lower() != WEBHOOK_SECRET.lower():
        logging.warning(f"🚫 Unauthorized webhook attempt. Received: '{data.get('secret')}' Expected: '{WEBHOOK_SECRET}'")
        return jsonify({"error": "unauthorized"}), 401

    action = data.get("action", "").upper()
    symbol = data.get("ticker", "").upper()
    quantity = int(data.get("quantity", 100))
    signal_close = float(data.get("signal_close", 0))

    # Determine buffer
    buffer = get_buffer(signal_close)

    # Buy or sell logic
    if action == "BUY":
        limit_price = round(signal_close + buffer, 3)
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price
        )
        trading.submit_order(order_data)
        logging.info(f"✅ BUY order placed for {symbol} at ${limit_price} (buffer {buffer}).")

    elif action in ["SELL", "STOP"]:
        limit_price = round(signal_close - buffer, 3)
        try:
            trading.close_position(symbol)
            logging.info(f"🟥 Closed open position in {symbol} (forced exit).")
        except Exception as e:
            logging.warning(f"⚠️ No open position to close for {symbol}: {e}")

        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price
        )
        trading.submit_order(order_data)
        logging.info(f"✅ SELL/STOP order placed for {symbol} at ${limit_price} (buffer {buffer}).")

    else:
        logging.warning(f"⚠️ Unknown action: {action}")
        return jsonify({"error": "unknown action"}), 400

    return jsonify({"status": "ok"}), 200

# ─────────────────────────────────────────────
# Forced daily exit (19:59 close)
# ─────────────────────────────────────────────
def daily_exit_check():
    now = datetime.now().time()
    if now >= dtime(19, 59):
        positions = trading.get_all_positions()
        for pos in positions:
            trading.close_position(pos.symbol)
            logging.info(f"🕓 Auto-closed {pos.symbol} at 19:59pm.")

# ─────────────────────────────────────────────
# Periodic background check
# ─────────────────────────────────────────────
@app.before_request
def before_request():
    daily_exit_check()

# ─────────────────────────────────────────────
# Run app
# ─────────────────────────────────────────────
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
































































