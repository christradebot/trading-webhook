from flask import Flask, request, jsonify
import os, json
from alpaca_trade_api.rest import REST

app = Flask(__name__)

# === Environment Variables ===
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "mysecret")

# === Alpaca Connection ===
api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')

@app.route("/tv", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data received"}), 400

    # Validate secret
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Invalid secret"}), 403

    print("🚀 Received alert:", json.dumps(data, indent=2))
    event = data.get("event", "").upper()
    symbol = data.get("ticker", "")
    qty = float(data.get("qty", 100))
    price = float(data.get("price", 0))
    extended = data.get("extended_hours", True)

    try:
        if event == "BUY":
            api.submit_order(
                symbol=symbol,
                qty=qty,
                side="buy",
                type="limit",
                time_in_force="gtc",
                limit_price=price,
                extended_hours=extended
            )
            print(f"✅ BUY placed for {symbol} at ${price}")

        elif "EXIT" in event or "STOP" in event:
            api.close_position(symbol)
            print(f"🛑 EXIT/STOP executed for {symbol}")

        else:
            print(f"⚠️ Unknown event type: {event}")

    except Exception as e:
        print(f"❌ ERROR: {e}")

    return jsonify({"status": "ok"}), 200


# === Railway entrypoint ===
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))












