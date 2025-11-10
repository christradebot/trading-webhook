import os
import json
import time
import logging
from datetime import datetime, time as dtime
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────
logging.basicConfig(
    format='[%(asctime)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    level=logging.INFO
)

app = Flask(__name__)

# ─────────────────────────────────────────────
# Load Railway environment variables (APCA_)
# ─────────────────────────────────────────────
API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
BASE_URL = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

if not WEBHOOK_SECRET:
    raise ValueError("🚨 WEBHOOK_SECRET missing in Railway Variables.")

logging.info(f"🔐 Loaded webhook secret from environment: '{WEBHOOK_SECRET}'")
logging.info(f"✅ Alpaca keys loaded successfully (Base URL: {BASE_URL})")

# ─────────────────────────────────────────────
# Alpaca client
# ─────────────────────────────────────────────
trading = TradingClient(API_KEY, SECRET_KEY, paper=True)

# ─────────────────────────────────────────────
# Helper
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

    # Case-insensitive secret check
    if data.get("secret", "").lower() != WEBHOOK_SECRET.lower():
        logging.warning(f"🚫 Unauthorized webhook attempt. Received: '{data.get('secret')}' Expected: '{WEBHOOK_SECRET}'")
        return jsonify({"error": "unauthorized"}), 401

    action = data.get("action", "").upper()
    symbol = data.get("ticker", "").upper()
    quantity = int(data.get("quantity", 100))
    signal_close = float(data.get("signal_close", 0))
    buffer = get_buffer(signal_close)

    try:
        if action == "BUY":
            limit_price = round(signal_close + buffer, 3)
            order = LimitOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price
            )
            trading.submit_order(order)
            logging.info(f"✅ BUY {symbol} at ${limit_price} (buffer {buffer})")

        elif action in ["SELL", "STOP"]:
            limit_price = round(signal_close - buffer, 3)
            order = LimitOrderRequest(
                symbol=symbol,
                qty=quantity,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price
            )
            trading.submit_order(order)
            logging.info(f"🟥 SELL/STOP {symbol} at ${limit_price} (buffer {buffer})")

        else:
            logging.warning(f"⚠️ Unknown action: {action}")
            return jsonify({"error": "unknown action"}), 400

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        logging.error(f"❌ Order error: {e}")
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────
# Forced daily exit at 19:59
# ─────────────────────────────────────────────
def daily_exit_check():
    now = datetime.now().time()
    if now >= dtime(19, 59):
        try:
            positions = trading.get_all_positions()
            for pos in positions:
                trading.close_position(pos.symbol)
                logging.info(f"🕓 Auto-closed {pos.symbol} at 19:59pm.")
        except Exception as e:
            logging.warning(f"⚠️ Auto-close error: {e}")

@app.before_request
def before_request():
    daily_exit_check()

# ─────────────────────────────────────────────
# Run server
# ─────────────────────────────────────────────
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)

































































