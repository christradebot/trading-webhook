from flask import Flask, request, jsonify
import os
from datetime import datetime

app = Flask(__name__)

# ──────────────────────────────────────────────
# Root route — health check
# ──────────────────────────────────────────────
@app.route('/')
def home():
    return "✅ TradingView Webhook Receiver Active — /tv endpoint ready"

# ──────────────────────────────────────────────
# TradingView / ReqBin POST endpoint
# ──────────────────────────────────────────────
@app.route('/tv', methods=['POST'])
def tradingview_webhook():
    try:
        # Parse JSON payload
        data = request.get_json(force=True)

        secret = data.get("secret", "")
        action = data.get("action", "").upper()
        ticker = data.get("ticker", "").upper()
        qty = data.get("quantity", "")
        signal_close = data.get("signal_close", "")
        source = data.get("source", "")

        # Validate secret key
        if secret != "chrisbot1501":
            print(f"❌ Unauthorized attempt with secret: {secret}")
            return jsonify({"status": "error", "message": "Unauthorized webhook attempt"}), 401

        # Log the webhook data
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        print(f"\n📩 [{now}] Webhook received:")
        print(f"Action: {action} | Ticker: {ticker} | Qty: {qty} | Source: {source} | Signal Close: {signal_close}")

        # Simulate acknowledgment (ready for Alpaca logic later)
        response_msg = {
            "status": "success",
            "timestamp": now,
            "action": action,
            "ticker": ticker,
            "quantity": qty,
            "source": source,
            "signal_close": signal_close
        }

        return jsonify(response_msg), 200

    except Exception as e:
        print(f"⚠️ Error handling webhook: {e}")
        return jsonify({"status": "error", "message": str(e)}), 400


# ──────────────────────────────────────────────
# Run Flask on Railway
# ──────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

























































