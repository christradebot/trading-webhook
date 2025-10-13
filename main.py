import os
import json
import logging
from flask import Flask, request, jsonify

app = Flask(__name__)

# ----- Logging so Railway "Logs" shows useful info -----
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webhook")

def make_alpaca():
    """Create Alpaca REST client on-demand with validation."""
    from alpaca_trade_api import REST

    key_id = os.getenv("APCA_API_KEY_ID")
    secret = os.getenv("APCA_API_SECRET_KEY")
    base_url = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

    missing = [n for n, v in [
        ("APCA_API_KEY_ID", key_id),
        ("APCA_API_SECRET_KEY", secret)
    ] if not v]
    if missing:
        raise ValueError(f"Missing Alpaca env vars: {', '.join(missing)}")

    return REST(key_id, secret, base_url, api_version="v2")

@app.route("/", methods=["GET"])
def health():
    return "Webhook is running ✅", 200

@app.route("/echo", methods=["POST"])
def echo():
    """Debug helper: shows exactly what TradingView sent."""
    try:
        data = request.get_json(force=True, silent=False)
    except Exception as e:
        log.exception("Failed to parse JSON")
        return jsonify(error=f"Invalid JSON: {e}"), 400
    log.info(f"/echo received: %s", data)
    return jsonify(received=data), 200

@app.route("/order", methods=["POST"])
def order():
    """
    Expected JSON from TradingView (examples):
    {
      "side": "buy",
      "symbol": "BTC/USD",
      "notional": 25           // USD amount for crypto (preferred) OR
      // "qty": 0.001          // alternative: crypto quantity
      // "type": "market",     // optional (market|limit), default market
      // "limit_price": 60000, // required if type == limit
      // "time_in_force": "gtc"// optional
    }
    """
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception as e:
        log.exception("Bad JSON")
        return jsonify(error=f"Invalid JSON: {e}"), 400

    # Basic validation
    side = str(payload.get("side", "")).lower()
    symbol = payload.get("symbol")
    order_type = str(payload.get("type", "market")).lower()
    notional = payload.get("notional")
    qty = payload.get("qty")
    tif = str(payload.get("time_in_force", "gtc")).lower()
    limit_price = payload.get("limit_price")

    if side not in {"buy", "sell"}:
        return jsonify(error="side must be 'buy' or 'sell'"), 400
    if not symbol:
        return jsonify(error="symbol is required, e.g. 'BTC/USD'"), 400
    if order_type not in {"market", "limit"}:
        return jsonify(error="type must be 'market' or 'limit'"), 400
    if order_type == "limit" and limit_price is None:
        return jsonify(error="limit_price required for type 'limit'"), 400

    # Prefer 'notional' for crypto; fallback to 'qty'
    if notional is None and qty is None:
        return jsonify(error="Provide 'notional' (USD) or 'qty'"), 400

    try:
        api = make_alpaca()
        log.info("Placing order: side=%s symbol=%s type=%s tif=%s notional=%s qty=%s",
                 side, symbol, order_type, tif, notional, qty)

        # Build request for Alpaca v2
        # Crypto runs 24/7; no extended_hours flag needed
        params = dict(
            side=side,
            symbol=symbol,
            type=order_type,
            time_in_force=tif,
            client_order_id=payload.get("client_order_id")
        )
        if order_type == "limit":
            params["limit_price"] = str(limit_price)

        # Prefer notional for crypto:
        if notional is not None:
            params["notional"] = str(notional)
        else:
            params["qty"] = str(qty)

        order = api.submit_order(**params)
        log.info("Order accepted: %s", order)
        return jsonify(status="ok", id=order.id, submitted=order._raw), 200

    except Exception as e:
        # Don’t crash the app; just return an error and log details
        log.exception("Order error")
        return jsonify(error=str(e)), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))







