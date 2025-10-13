from flask import Flask, request, jsonify
import alpaca_trade_api as tradeapi
import os

app = Flask(__name__)

# ========================================
# 🔑 Alpaca API Keys (from Railway environment variables)
# ========================================
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"  # Paper trading endpoint

# Initialize API
api = tradeapi.REST(ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')


# ========================================
# 🚀 Webhook Route
# ========================================
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()

        symbol = data.get('symbol', 'BTC/USD')     # default to BTC
        action = data.get('action', '').lower()
        qty = float(data.get('qty', 0.001))        # default 0.001 BTC
        order_type = data.get('type', 'market')    # can be 'market' or 'limit'
        limit_price = data.get('limit_price', None)

        print(f"🔔 Webhook received: {action.upper()} {symbol} qty={qty} type={order_type}")

        if action not in ['buy', 'sell']:
            return jsonify({"status": "error", "message": "Invalid action. Use 'buy' or 'sell'."}), 400

        # ========== Place Order ==========
        if order_type == 'limit' and limit_price:
            api.submit_order(
                symbol=symbol,
                qty=qty,
                side=action,
                type='limit',
                time_in_force='day',
                limit_price=float(limit_price),
                extended_hours=True
            )
            print(f"✅ {action.upper()} LIMIT order placed for {symbol} at {limit_price}")
            return jsonify({"status": "success", "message": f"{action.upper()} LIMIT order placed at {limit_price}"})

        else:
            api.submit_order(
                symbol=symbol,
                qty=qty,
                side=action,
                type='market',
                time_in_force='gtc',
                extended_hours=True
            )
            print(f"✅ {action.upper()} MARKET order placed for {symbol} ({qty})")
            return jsonify({"status": "success", "message": f"{action.upper()} MARKET order placed for {symbol} ({qty})"})

    except Exception as e:
        print(f"❌ Error processing webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ========================================
# 🏠 Root Route
# ========================================
@app.route('/')
def home():
    return "TradingView → Railway → Alpaca Webhook (extended hours enabled) ✅"


# ========================================
# 🧠 Main Entry Point
# ========================================
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)

