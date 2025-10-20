# © Chris / Athena
# main.py — TradingView → Alpaca Webhook Bridge (EMA20 Logic + Synthetic Stops)

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST, TimeFrame
import os, json, math, datetime, time

app = Flask(__name__)

#──────────────────────────────
# Environment Variables
#──────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "mysecret")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')

#──────────────────────────────
# Helpers
#──────────────────────────────
def round_price(symbol, price):
    """Round price to valid increments for Alpaca (avoids sub-penny errors)."""
    if price < 1:
        return round(price, 4)
    elif price < 10:
        return round(price, 3)
    else:
        return round(price, 2)

def percent_diff(a, b):
    """Calculate percentage difference between two prices."""
    return abs((a - b) / b) * 100

#──────────────────────────────
# Flask Routes
#──────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, service="tv→alpaca", base=ALPACA_BASE_URL)

@app.post("/tv")
def tv():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403

    print("🚀 TradingView Alert:", json.dumps(data, indent=2))
    action = data.get("action", "").upper()
    symbol = data.get("ticker", "").upper()
    price  = float(data.get("price", 0))

    # safely parse ema20 — handles strings like '{{plot("EMA 20")}}'
    try:
        ema20 = float(data.get("ema20", 0))
    except (ValueError, TypeError):
        ema20 = 0.0

    #──────────────────────────────
    # EMA Proximity Filter (max 3%)
    #──────────────────────────────
    if ema20 > 0:
        dist = percent_diff(price, ema20)
        if dist > 3:
            print(f"⛔ Skipping {symbol}: {dist:.2f}% away from EMA20 ({ema20})")
            return jsonify(ignored=True, reason="too far from EMA20")

    qty = 100  # test quantity — adjust later if needed
    tp_mult = 1.10  # 10% trailing take profit
    sl_mult = 0.95  # 5% stop loss

    try:
        #──────────────────────────────
        # BUY ENTRY LOGIC
        #──────────────────────────────
        if action == "BUY":
            limit_price = round_price(symbol, price)
            print(f"📈 BUY {symbol} @ {limit_price}")

            api.submit_order(
                symbol=symbol,
                qty=qty,
                side="buy",
                type="limit",
                limit_price=limit_price,
                time_in_force="day",
                extended_hours=True  # ✅ allows premarket execution
            )

            # Synthetic TP & SL (trailing style)
            tp_price = round_price(symbol, price * tp_mult)
            sl_price = round_price(symbol, price * sl_mult)

            print(f"💰 Synthetic TP: {tp_price} | 🛑 Stop: {sl_price}")
            monitor_trade(symbol, price, tp_price, sl_price)

        #──────────────────────────────
        # EXIT / TAKE PROFIT
        #──────────────────────────────
        elif action in ["EXIT", "SELL", "TP"]:
            print(f"🔻 EXIT signal for {symbol}")
            close_position(symbol)

        else:
            print("⚠️ Unknown action in alert.")
            return jsonify(ok=False, reason="unknown action")

    except Exception as e:
        print(f"❌ ERROR processing {symbol}: {e}")
        return jsonify(error=str(e)), 500

    return jsonify(ok=True)

#──────────────────────────────
# Synthetic Exit Management
#──────────────────────────────
def monitor_trade(symbol, entry_price, tp_price, sl_price):
    """
    Lightweight synthetic monitor (pre-market safe)
    Checks every few seconds and executes exits manually.
    """
    print(f"🕒 Monitoring {symbol}... (synthetic TP/SL active)")

    for _ in range(60):  # up to ~5 min loop
        try:
            barset = api.get_bars(symbol, TimeFrame.Minute, limit=1)
            if not barset:
                time.sleep(5)
                continue

            current_price = float(barset[-1].c)
            if current_price >= tp_price:
                print(f"✅ TP hit {symbol} ({current_price})")
                close_position(symbol)
                return
            elif current_price <= sl_price:
                print(f"🛑 Stop hit {symbol} ({current_price})")
                close_position(symbol)
                return
        except Exception as e:
            print(f"⚠️ Monitor error: {e}")

        time.sleep(5)

def close_position(symbol):
    """Safely closes any open position."""
    try:
        pos = api.get_position(symbol)
        if pos and float(pos.qty) > 0:
            qty = abs(float(pos.qty))
            print(f"💥 Closing {symbol}, {qty} shares")
            api.submit_order(
                symbol=symbol,
                qty=qty,
                side="sell",
                type="market",
                time_in_force="day",
                extended_hours=True
            )
    except Exception as e:
        print(f"⚠️ Close error: {e}")

#──────────────────────────────
# Run Server
#──────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)



















