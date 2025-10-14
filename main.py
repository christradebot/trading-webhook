from flask import Flask, request, jsonify
import os
import alpaca_trade_api as tradeapi

app = Flask(__name__)

@app.route("/", methods=["GET"])
def home():
    return "Webhook is running with Alpaca crypto test ✅"

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    print("Received data:", data)

    if not data:
        return jsonify({"status": "error", "message": "No JSON data received"}), 400

    try:
        # --- Use Alpaca's Crypto endpoint for 24/7 testing ---
        api = tradeapi.REST(
            os.environ.get("APCA_API_KEY_ID"),
            os.environ.get("APCA_API_SECRET_KEY"),
            "https://paper-api.alpaca.markets/v1beta1/crypto",
            api_version="v2"
        )

        side = data.get("side", "buy")
        symbol = data.get("symbol", "BTC/USD")
        order_type = data.get("type", "market")
        notional = float(data.get("notional", 1))

        print(f"Submitting {side} order for {symbol} (notional: {notional})...")

        order = api.submit_order(
            symbol=symbol,
            side=side,
            type=order_type,
            notional=notional,
            time_in_force="gtc"  # works for crypto
        )

        print("Order submitted successfully:", order)
        return jsonify({
            "status": "success",
            "message": "✅ Alpaca crypto order test successful!",
            "symbol": symbol,
            "side": side,
            "order_id": order.id
        }), 200

    except Exception as e:
        print("Error submitting order:", e)
        return jsonify({
            "status": "error",
            "message": f"❌ Alpaca crypto order test failed: {str(e)}"
        }), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))










