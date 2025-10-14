from flask import Flask, request, jsonify
import os
import alpaca_trade_api as tradeapi

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Webhook is running ✅ Connected to Alpaca /v2"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("Received:", data)

    if not data:
        return jsonify({"status": "error", "message": "No JSON payload"}), 400

    try:
        side = data.get("side", "buy")
        symbol = data.get("symbol", "BTC/USD")
        order_type = data.get("type", "market")
        notional = float(data.get("notional", 1))
        time_in_force = data.get("time_in_force", "gtc")

        # Use your base API URL (no /v2 here!)
        base_url = os.environ.get("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

        api = tradeapi.REST(
            os.environ.get("APCA_API_KEY_ID"),
            os.environ.get("APCA_API_SECRET_KEY"),
            base_url,
            api_version="v2"
        )

        print(f"Placing {side.upper()} order for {symbol} at {base_url}/v2/orders")

        order = api.submit_order(
            symbol=symbol,
            side=side,
            type=order_type,
            notional=notional,
            time_in_force=time_in_force
        )

        return jsonify({
            "status": "success",
            "message": f"✅ {symbol} order sent successfully!",
            "order_id": order.id
        }), 200

    except Exception as e:
        print("Error:", e)
        return jsonify({
            "status": "error",
            "message": f"❌ Alpaca order test failed: {str(e)}"
        }), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))











