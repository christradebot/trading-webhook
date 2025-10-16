# main.py
from flask import Flask, request, jsonify
import os, json
from alpaca_trade_api.rest import REST

app = Flask(__name__)

# ---- Environment ----
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "mysecret")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')

@app.get("/ping")
def ping():
    return jsonify(ok=True, service="tv→alpaca", base=ALPACA_BASE_URL)

@app.post("/tv")
def tv():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403

    print("🚀 Alert:", json.dumps(data, indent=2))
    event  = str(data.get("event", "")).upper()
    symbol = str(data.get("ticker", ""))
    qty    = int(float(data.get("qty", 100)))
    price  = float(data.get("price", 0))
    extended = bool(data.get("extended_hours", True))

    try:
        if event == "BUY":
            api.submit_order(
                symbol=symbol,
                qty=qty,
                side="buy",
                type="limit",
                time_in_force="day",
                limit_price=price,
                extended_hours=extended
            )
            msg = f"BUY submitted {symbol} {qty}@{price}"
        elif "EXIT" in event or "STOP" in event:
            api.close_position(symbol)
            msg = f"EXIT/STOP submitted for {symbol}"
        else:
            msg = f"Unknown event: {event}"
        print("✅", msg)
        return jsonify(status="ok", message=msg)
    except Exception as e:
        print("❌ Broker error:", e)
        return jsonify(error=str(e)), 502

if __name__ == "__main__":
    # For local dev; Railway will use Procfile (gunicorn)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))













