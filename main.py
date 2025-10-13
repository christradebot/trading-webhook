from flask import Flask, request, jsonify
import os
import alpaca_trade_api as tradeapi

app = Flask(__name__)

# ✅ Initialize Alpaca API
API_KEY = os.environ.get("ALPACA_API_KEY_ID")
API_SECRET = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
api = tradeapi.REST(API_KEY, API_SECRET, BASE_URL, api_version='v2')


@app.route("/", methods=["GET"])
def home():
    return "Webhook is running ✅"


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        print("🚀 Incoming webhook data:", data, flush=True)

        if not data:
            return jsonify({"status": "no data"}), 400

        action = data.get("action")
        symbol = data.get("symbol")
        qty = float(data.get("qty", 0))

        if action == "buy":
            api.submit_order(symbol=symbol, qty=qty, side='buy', type='market', time_in_force='gtc')
            print(f"✅ Buy order placed for {qty} {symbol}", flush=True)
        elif action == "sell":
            api.submit_order(symbol=symbol, qty=qty, side='sell', type='market', time_in_force='gtc')
            print(f"✅ Sell order placed for {qty} {symbol}", flush=True)
        else:
            print("⚠️ Unknown action:", action, flush=True)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print("❌ Error in webhook:", e, flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))






