from flask import Flask, request, jsonify
import os
import alpaca_trade_api as tradeapi

app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    return "Webhook is running with Alpaca test ✅"

@app.route('/order', methods=['POST'])
def create_order():
    try:
        data = request.get_json()
        side = data.get("side", "buy")
        symbol = data.get("symbol", "BTC/USD")
        type_ = data.get("type", "market")
        notional = data.get("notional", 2)

        # Load Alpaca credentials from Railway environment
        key_id = os.getenv("APCA_API_KEY_ID")
        secret_key = os.getenv("APCA_API_SECRET_KEY")
        base_url = os.getenv("APCA_API_BASE_URL")

        if not key_id or not secret_key or not base_url:
            raise ValueError("Missing Alpaca environment variables")

        api = tradeapi.REST(key_id, secret_key, base_url, api_version='v2')

        order = api.submit_order(
            symbol=symbol,
            side=side,
            type=type_,
            notional=notional
        )

        return jsonify({
            "status": "ok",
            "message": "Order sent to Alpaca ✅",
            "alpaca_response": order._raw
        }), 200

    except Exception as e:
        print(f"Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=5000)








