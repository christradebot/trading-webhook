from flask import Flask, request, jsonify
import os
import alpaca_trade_api as tradeapi

app = Flask(__name__)

# === Alpaca environment variables ===
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

# === Initialize Alpaca client ===
try:
    api = tradeapi.REST(API_KEY, SECRET_KEY, BASE_URL, api_version='v2')
    account = api.get_account()
    print(f"✅ Connected to Alpaca account: {account.id}")
except Exception as e:
    print(f"⚠️ Error connecting to Alpaca: {e}")

# === Home route (test page) ===
@app.route('/', methods=['GET'])
def home():
    return "Webhook is running ✅"

# === Webhook endpoint ===
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()

        # Example: handle TradingView alerts
        if data and 'side' in data and 'symbol' in data:
            symbol = data['symbol']
            side = data['side'].lower()
            qty = float(data.get('qty', 0.001))  # default test quantity

            if side == 'buy':
                api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side='buy',
                    type='market',
                    time_in_force='gtc'
                )
                return jsonify({'status': 'ok', 'message': f'Buy order placed for {qty} {symbol}'}), 200

            elif side == 'sell':
                api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side='sell',
                    type='market',
                    time_in_force='gtc'
                )
                return jsonify({'status': 'ok', 'message': f'Sell order placed for {qty} {symbol}'}), 200

        return jsonify({'status': 'ignored', 'message': 'Invalid or missing fields'}), 400

    except Exception as e:
        print(f"Webhook error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# === Run app ===
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))


