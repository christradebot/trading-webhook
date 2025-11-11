# =====================================================
# main.py — Final Stable Version (Chris + Athena)
# =====================================================

import os
import json
import time
from datetime import datetime
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# =====================================================
# Alpaca Credentials (locked variable names)
# =====================================================
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)

# =====================================================
# Flask app
# =====================================================
app = Flask(__name__)

# =====================================================
# Config
# =====================================================
ENTRY_BUFFER_LOW = 0.03      # For tickers under $1
ENTRY_BUFFER_HIGH = 0.003    # For tickers $1 or above
SELL_RETRY_INTERVAL = 3      # Seconds between sell retries
SELL_MAX_RETRIES = 20        # Try for 1 minute total

# =====================================================
# Utility Functions
# =====================================================
def get_entry_price(close_price):
    """Apply correct entry buffer."""
    close_price = float(close_price)
    if close_price >= 1:
        return round(close_price + ENTRY_BUFFER_HIGH, 4)
    else:
        return round(close_price + ENTRY_BUFFER_LOW, 4)

def place_buy(symbol, qty, limit_price, source):
    """Place a buy limit order."""
    try:
        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            type="limit",
            time_in_force=TimeInForce.DAY,
            limit_price=limit_price
        )
        trading_client.submit_order(order)
        print(f"✅ BUY placed {symbol} x{qty} @ {limit_price} ({source})")
        return True
    except Exception as e:
        print(f"❌ BUY failed for {symbol}: {e}")
        return False

def place_sell(symbol, qty, limit_price, source):
    """Keep trying until sell fills."""
    for attempt in range(SELL_MAX_RETRIES):
        try:
            order = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                type="limit",
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price
            )
            trading_client.submit_order(order)
            print(f"✅ SELL placed {symbol} x{qty} @ {limit_price} ({source})")
            return True
        except Exception as e:
            print(f"⚠️ SELL attempt {attempt+1} failed for {symbol}: {e}")
            time.sleep(SELL_RETRY_INTERVAL)
    print(f"❌ SELL failed for {symbol} after retries.")
    return False

# =====================================================
# Webhook endpoint
# =====================================================
@app.route('/tv', methods=['POST'])
def tv():
    if not request.is_json:
        return jsonify({"error": "415 Unsupported Media Type — send JSON"}), 415

    payload = request.get_json(force=True)
    print(f"🔍 Raw webhook body: {json.dumps(payload, indent=2)}")

    secret = payload.get("secret")
    if secret != "CHRISBOT1501":
        return jsonify({"error": "❌ Unauthorized"}), 403

    action = payload.get("action")
    symbol = payload.get("ticker")
    qty = int(payload.get("quantity", 100))
    signal_close = payload.get("signal_close", 0.0)
    source = payload.get("source", "N/A")

    try:
        close_price = float(signal_close)
    except ValueError:
        print(f"⚠️ Invalid close price in payload: {signal_close}")
        return jsonify({"error": "Invalid close price"}), 400

    print(f"✅ Parsed payload: action={action} symbol={symbol} close={close_price} qty={qty} source={source}")

    if action == "BUY":
        limit_price = get_entry_price(close_price)
        print(f"🕒 Pending BUY for {symbol} x{qty} @ {limit_price} ({source})")
        place_buy(symbol, qty, limit_price, source)

    elif action == "SELL":
        limit_price = close_price
        print(f"🕒 Attempting SELL for {symbol} x{qty} @ {limit_price} ({source})")
        place_sell(symbol, qty, limit_price, source)

    else:
        print(f"⚠️ Unknown action: {action}")
        return jsonify({"error": "Invalid action"}), 400

    return jsonify({"status": "ok", "symbol": symbol, "action": action})

# =====================================================
# Run
# =====================================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)















































































