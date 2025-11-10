# ===============================================================
# ChrisBot 1501 — Alpaca Trading Webhook Server (FINAL VERIFIED)
# ===============================================================

import os
from datetime import datetime
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ── ENVIRONMENT VARIABLES ──────────────────────────────────────
# ✅ Make sure these are set in Railway exactly as below:
# APCA_API_KEY_ID
# APCA_API_SECRET_KEY
# APCA_API_BASE_URL
# WEBHOOK_SECRET  (example: CHRISBOT1501)

API_KEY        = os.getenv("APCA_API_KEY_ID")
SECRET_KEY     = os.getenv("APCA_API_SECRET_KEY")
BASE_URL       = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

# ── INITIALIZE ALPACA & FLASK ──────────────────────────────────
trading = TradingClient(API_KEY, SECRET_KEY, paper=("paper" in BASE_URL))
app = Flask(__name__)

# ── STATE VARIABLES ────────────────────────────────────────────
open_positions = {}
loss_counter = {}
MAX_LOSSES_PER_TICKER = 2


# ── UTILITIES ─────────────────────────────────────────────────
def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def log(msg):
    print(f"[{now()}] {msg}", flush=True)

def submit_limit(symbol, side, qty, price):
    """Submit a limit order with full error handling"""
    try:
        order = trading.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                limit_price=price,
                time_in_force=TimeInForce.DAY
            )
        )
        log(f"✅ {side.upper()} {symbol} x{qty} @ {price}")
        return order
    except Exception as e:
        log(f"❌ {side.upper()} {symbol} order failed: {e}")
        return None

def close_all_positions_at_1959():
    """Force close all open trades at 19:59 UTC"""
    t = datetime.utcnow()
    if t.hour == 19 and t.minute >= 59:
        log("🕘 19:59 UTC reached — closing all open positions...")
        try:
            for pos in trading.get_all_positions():
                try:
                    trading.close_position(pos.symbol)
                    log(f"🔻 Closed {pos.symbol}")
                except Exception as e:
                    log(f"⚠️ Could not close {pos.symbol}: {e}")
        except Exception as e:
            log(f"⚠️ Could not fetch positions: {e}")


# ── ROUTES ────────────────────────────────────────────────────
@app.get("/")
def root():
    return jsonify({
        "status": "alive",
        "service": "ChrisBot1501",
        "apca_api_base_url": BASE_URL,
        "apca_api_key_id": API_KEY[:6] + "****",
        "apca_api_secret_key": "********",
        "webhook_secret": WEBHOOK_SECRET
    })

@app.get("/health")
def health():
    return jsonify({"ok": True, "time": now()})

@app.post("/TV")
def tv_webhook():
    data = request.get_json(force=True)
    log(f"📩 Webhook received: {data}")

    # Validate webhook secret
    if data.get("SECRET", "").upper() != WEBHOOK_SECRET.upper():
        log("🚫 Unauthorized webhook attempt.")
        return jsonify({"error": "unauthorized"}), 401

    # Parse fields
    action = (data.get("ACTION") or "").upper()
    symbol = (data.get("TICKER") or "").upper()
    qty    = int(data.get("QUANTITY") or 0)
    sig_close = float(data.get("SIGNAL_CLOSE") or 0)
    src    = data.get("SOURCE", "UNKNOWN")

    if not symbol or qty <= 0:
        return jsonify({"error": "invalid ticker/quantity"}), 400

    # Check time safeguard
    close_all_positions_at_1959()

    # ─ BUY LOGIC ─
    if action == "BUY":
        if loss_counter.get(symbol, 0) >= MAX_LOSSES_PER_TICKER:
            log(f"⚠️ Skipping BUY {symbol}: max losses reached.")
            return jsonify({"status": "skipped"}), 200

        buffer = 0.03 if sig_close >= 1 else 0.003
        entry_price = round(sig_close + buffer, 4)
        submit_limit(symbol, OrderSide.BUY, qty, entry_price)
        open_positions[symbol] = {"entry": entry_price, "qty": qty}
        log(f"🟢 BUY signal confirmed ({src}) at {entry_price}")
        return jsonify({"ok": True, "buy_price": entry_price}), 200

    # ─ SELL / STOP LOGIC ─
    if action in ("SELL", "STOP"):
        pos = open_positions.pop(symbol, {"qty": qty})
        sell_price = round(sig_close, 4)
        submit_limit(symbol, OrderSide.SELL, pos["qty"], sell_price)
        loss_counter[symbol] = loss_counter.get(symbol, 0) + 1
        log(f"🔴 SELL/STOP executed ({src}) at {sell_price}")
        return jsonify({"ok": True, "sell_price": sell_price}), 200

    return jsonify({"error": "invalid action"}), 400


# ── SERVER STARTUP ─────────────────────────────────────────────
if __name__ == "__main__":
    log("🚀 Launching ChrisBot1501 (FINAL STABLE VERSION)...")
    app.run(host="0.0.0.0", port=8080)





































































