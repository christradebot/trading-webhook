# main.py
# --------------------------------------
# ✅ FINAL STABLE BUILD (Chris + Athena)
# Alpaca Limit-Only Webhook Trading Bot
# --------------------------------------

import os
import json
import time
from datetime import datetime
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType
from alpaca.trading.requests import LimitOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# --------------------------------------
# Environment Variables (locked names)
# --------------------------------------
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

if not all([API_KEY, SECRET_KEY, BASE_URL, WEBHOOK_SECRET]):
    raise ValueError("🚨 Missing one or more Alpaca or Webhook environment variables.")

# --------------------------------------
# Alpaca Clients
# --------------------------------------
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# --------------------------------------
# Flask App
# --------------------------------------
app = Flask(__name__)

# --------------------------------------
# Helper Functions
# --------------------------------------
def get_live_price(symbol: str) -> float:
    """Fetch the most recent quote for precise pricing."""
    try:
        quote_req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quote = data_client.get_stock_latest_quote(quote_req)
        if symbol in quote:
            return float(quote[symbol].ask_price or quote[symbol].bid_price or 0)
    except Exception as e:
        print(f"⚠️ Live price fetch failed for {symbol}: {e}")
    return 0.0


def place_limit_order(symbol: str, qty: int, side: str, limit_price: float, source: str):
    """Submit limit-only order."""
    try:
        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL,
            limit_price=limit_price,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order)
        print(f"✅ {side.upper()} placed {symbol} x{qty} @ {limit_price} (source={source})")
        return True
    except Exception as e:
        print(f"❌ Order placement failed: {e}")
        return False


def cancel_open_orders(symbol: str):
    """Cancel any unfilled open orders for this ticker."""
    try:
        open_orders = trading_client.get_orders(status="open")
        for order in open_orders:
            if order.symbol == symbol:
                trading_client.cancel_order_by_id(order.id)
                print(f"🧹 Cancelled open order for {symbol}")
    except Exception as e:
        print(f"⚠️ Order cancellation error: {e}")


def close_position(symbol: str):
    """Keep retrying until position fully closed."""
    retries = 0
    while retries < 10:
        try:
            positions = trading_client.get_all_positions()
            for pos in positions:
                if pos.symbol == symbol:
                    print(f"🚨 Closing {symbol} position...")
                    order = LimitOrderRequest(
                        symbol=symbol,
                        qty=abs(int(float(pos.qty))),
                        side=OrderSide.SELL,
                        limit_price=get_live_price(symbol) * 0.99,  # sell slightly below market
                        time_in_force=TimeInForce.DAY
                    )
                    trading_client.submit_order(order)
                    time.sleep(3)
            # verify position closed
            remaining = [p for p in trading_client.get_all_positions() if p.symbol == symbol]
            if not remaining:
                print(f"✅ Position fully closed for {symbol}")
                return True
        except Exception as e:
            print(f"⚠️ Error while closing {symbol}: {e}")
        retries += 1
        time.sleep(2)
    print(f"❌ Failed to close {symbol} after {retries} retries.")
    return False


# --------------------------------------
# Webhook Endpoint
# --------------------------------------
@app.route("/tv", methods=["POST"])
def tv():
    if request.content_type != "application/json":
        return jsonify({"error": "415 Unsupported Media Type"}), 415

    payload = request.get_json(silent=True)
    if not payload:
        return jsonify({"error": "Invalid or empty payload", "ok": False}), 400

    # Secret Validation
    if payload.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized webhook secret", "ok": False}), 403

    action = payload.get("action", "").upper()
    symbol = payload.get("ticker", "").upper().replace("{{", "").replace("}}", "")
    qty = int(payload.get("quantity", 0))
    source = payload.get("source", "unknown")

    if not symbol or "TICKER" in symbol or "{" in symbol:
        return jsonify({"error": "Invalid or placeholder ticker", "ok": False}), 400

    live_price = get_live_price(symbol)
    if live_price <= 0:
        return jsonify({"error": f"Invalid live price for {symbol}", "ok": False}), 400

    # Entry buffer (fixed decimal, not percent)
    entry_buffer = 0.03 if live_price >= 1 else 0.003
    limit_price = round(live_price + entry_buffer, 4) if action == "BUY" else round(live_price - entry_buffer, 4)

    print(f"🔍 Parsed payload: {payload}")
    print(f"📈 Live price for {symbol}: {live_price}, limit price: {limit_price}")

    if action == "BUY":
        cancel_open_orders(symbol)
        placed = place_limit_order(symbol, qty, "BUY", limit_price, source)
        if placed:
            # wait one bar (1 minute typically)
            time.sleep(60)
            cancel_open_orders(symbol)
        return jsonify({"ok": placed, "symbol": symbol, "limit_price": limit_price}), 200

    elif action == "SELL":
        success = close_position(symbol)
        return jsonify({"ok": success, "symbol": symbol}), 200

    else:
        return jsonify({"error": "Unknown action", "ok": False}), 400


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})


# --------------------------------------
# Run
# --------------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)















































































