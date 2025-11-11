# ============================================================
#  ChrisBot Trading Webhook (Final Build)
#  - Limit orders only (no market orders)
#  - Instant execution on alert (no next-bar delay)
#  - Live pricing via Alpaca quotes
#  - 3¢ buffer above $1, 0.3¢ buffer below $1
#  - Forced sell completion loop
#  - Logs every trade
# ============================================================

import os, time, json
from datetime import datetime
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ------------------------------------------------------------
# Load environment variables (must match Railway exactly)
# ------------------------------------------------------------
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

# ------------------------------------------------------------
# Initialize Alpaca clients
# ------------------------------------------------------------
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

app = Flask(__name__)

# ------------------------------------------------------------
# Utility: get live quote price
# ------------------------------------------------------------
def get_live_price(symbol: str, action: str, fallback_close: float):
    try:
        q = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol=symbol))
        ref = q.ask_price if action == "BUY" else q.bid_price
        if ref is None or ref == 0:
            print(f"⚠️ No live quote for {symbol}, using fallback close {fallback_close}")
            return fallback_close
        return ref
    except Exception as e:
        print(f"⚠️ Quote error for {symbol}: {e}")
        return fallback_close

# ------------------------------------------------------------
# Utility: place limit order with buffer
# ------------------------------------------------------------
def place_limit_order(symbol, qty, action, close_price, source):
    ref = get_live_price(symbol, action, close_price)
    buf = 0.03 if ref >= 1.0 else 0.003
    limit_price = round(ref + buf, 4) if action == "BUY" else round(ref - buf, 4)

    side = OrderSide.BUY if action == "BUY" else OrderSide.SELL
    print(f"🔹 Preparing {action} {symbol} x{qty} | ref={ref} → limit={limit_price}")

    order_data = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        type="limit",
        time_in_force=TimeInForce.DAY,
        limit_price=limit_price,
    )

    try:
        order = trading_client.submit_order(order_data)
        print(f"✅ {action} placed {symbol} x{qty} @ {limit_price} (source={source})")
        return order
    except Exception as e:
        print(f"❌ Error placing {action} for {symbol}: {e}")
        return None

# ------------------------------------------------------------
# Utility: force close position on SELL
# ------------------------------------------------------------
def force_close_position(symbol, qty, close_price):
    print(f"🚨 Ensuring full exit for {symbol} ({qty} shares)...")
    tries = 0
    while tries < 10:
        try:
            positions = trading_client.get_all_positions()
            open_pos = next((p for p in positions if p.symbol == symbol), None)
            if not open_pos:
                print(f"✅ Position fully closed for {symbol}")
                return True
            remaining = float(open_pos.qty)
            print(f"🔁 Attempt {tries+1}: still holding {remaining}, retrying sell...")
            place_limit_order(symbol, remaining, "SELL", close_price, "FORCE_CLOSE")
            time.sleep(10)
        except Exception as e:
            print(f"⚠️ Error checking/closing {symbol}: {e}")
        tries += 1
    print(f"⚠️ Gave up after 10 attempts to close {symbol}")
    return False

# ------------------------------------------------------------
# Utility: enforce daily close (19:59 ET)
# ------------------------------------------------------------
def close_all_positions():
    now = datetime.utcnow()
    if now.hour == 23 and now.minute >= 59:  # 19:59 ET ≈ 23:59 UTC
        print("🕘 Closing all positions before end of session...")
        try:
            trading_client.close_all_positions()
            print("✅ All positions closed successfully.")
        except Exception as e:
            print(f"⚠️ Error closing all positions: {e}")

# ------------------------------------------------------------
# Webhook Endpoint
# ------------------------------------------------------------
@app.route("/tv", methods=["POST"])
def tv():
    try:
        payload = request.get_json()
        print(f"🔍 Raw webhook body: {json.dumps(payload, indent=2)}")

        if payload.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized webhook secret"}), 401

        action = payload.get("action", "").upper()
        symbol = payload.get("ticker", "").upper()
        qty = int(payload.get("quantity", 0))
        signal_close = float(payload.get("signal_close", 0.0))
        source = payload.get("source", "unknown")

        if not symbol or "{" in symbol:
            return jsonify({"error": "Invalid or placeholder ticker", "ok": False}), 400

        print(f"✅ Parsed payload: {action=} {symbol=} {qty=} {source=}")

        if action == "BUY":
            place_limit_order(symbol, qty, "BUY", signal_close, source)

        elif action == "SELL":
            place_limit_order(symbol, qty, "SELL", signal_close, source)
            force_close_position(symbol, qty, signal_close)

        else:
            return jsonify({"error": "Invalid action"}), 400

        close_all_positions()  # run end-of-day check each time
        return jsonify({
            "ok": True,
            "scheduled_for": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
            "symbol": symbol
        }), 200

    except Exception as e:
        print(f"❌ Exception in /tv: {e}")
        return jsonify({"error": str(e)}), 500

# ------------------------------------------------------------
# Health check
# ------------------------------------------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})

# ------------------------------------------------------------
# Run
# ------------------------------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)











































































