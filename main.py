# main.py — Clean Alpaca Paper-Trading Webhook Bot
# Author: Chris + Athena (2025)
# Uses the official alpaca-py SDK — compatible with Railway deployment

import os
import json
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest

# ─────────────────────────────────────────────
# Flask App
# ─────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────
# Environment Variables (Railway-ready)
# ─────────────────────────────────────────────
API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

if not API_KEY or not SECRET_KEY:
    raise SystemExit("❌ Missing Alpaca API credentials — set APCA_API_KEY_ID and APCA_API_SECRET_KEY in Railway.")

# ─────────────────────────────────────────────
# Initialize Alpaca Clients
# ─────────────────────────────────────────────
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

try:
    account = trading_client.get_account()
    print(f"✅ Connected to Alpaca Paper Account — status: {account.status}, equity: ${account.equity}")
except Exception as e:
    print(f"❌ Failed to connect to Alpaca: {e}")

# ─────────────────────────────────────────────
# Helper: fetch latest quote (for limit orders)
# ─────────────────────────────────────────────
def get_latest_price(symbol: str) -> float:
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quote = data_client.get_stock_latest_quote(req)
        price = quote[symbol].ask_price or quote[symbol].bid_price
        print(f"💰 Latest {symbol} price: {price}")
        return float(price)
    except Exception as e:
        print(f"⚠️ Error fetching latest price for {symbol}: {e}")
        return None

# ─────────────────────────────────────────────
# Webhook endpoint (TradingView → Alpaca)
# ─────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.data.decode("utf-8"))
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    # Optional: simple security check
    if WEBHOOK_SECRET and data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    action = data.get("action")
    symbol = data.get("ticker") or data.get("symbol")
    qty = int(data.get("quantity", 1))
    side = OrderSide.BUY if action == "BUY" else OrderSide.SELL
    order_type = data.get("type", "market").lower()
    limit_price = None

    # Optional: get live market price if needed
    if order_type == "limit":
        limit_price = get_latest_price(symbol)

    print(f"📩 Webhook received: {data}")

    try:
        if order_type == "limit" and limit_price:
            order = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                limit_price=limit_price,
                time_in_force=TimeInForce.DAY
            )
        else:
            order = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY
            )

        trading_client.submit_order(order)
        print(f"✅ {action} order submitted: {symbol} x{qty}")
        return jsonify({"status": "success", "order": str(order)}), 200

    except Exception as e:
        print(f"❌ Order submission failed: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ─────────────────────────────────────────────
# Root route — sanity check
# ─────────────────────────────────────────────
@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "running", "account": str(account.status)}), 200

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)





















































