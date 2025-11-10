# ===============================================================
# ChrisBot 1502 — Alpaca Trading Webhook Server (Final Stable)
# ===============================================================

import os
import json
from datetime import datetime
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ──────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "chrisbot1501")

# Debug print for verification
print(f"🔐 Loaded webhook secret from environment: '{WEBHOOK_SECRET}'", flush=True)

# Validate Alpaca credentials
if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

# Initialize Trading Client
trading = TradingClient(API_KEY, SECRET_KEY, paper=("paper" in BASE_URL))

# ──────────────────────────────────────────────────────────────
# APP INITIALIZATION
# ──────────────────────────────────────────────────────────────
app = Flask(__name__)

open_positions = {}
loss_counter = {}

MAX_LOSSES_PER_TICKER = 2


# ──────────────────────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────────────────────
def now():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")


def log(msg):
    print(f"[{now()}] {msg}", flush=True)


def submit_limit_order(symbol, side, qty, price):
    """Submit a limit order to Alpaca"""
    try:
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=price,
            time_in_force=TimeInForce.DAY
        )
        order = trading.submit_order(order_data)
        log(f"✅ {side.upper()} {symbol} x{qty} @ {price}")
        return order
    except Exception as e:
        log(f"❌ Failed {side.upper()} for {symbol}: {e}")
        return None


def close_all_positions_at_1959():
    """Force close all open positions at 19:59 UTC"""
    now_utc = datetime.utcnow()
    if now_utc.hour == 19 and now_utc.minute >= 59:
        log("🕘 19:59 reached — closing all positions.")
        try:
            positions = trading.get_all_positions()
            for pos in positions:
                try:
                    trading.close_position(pos.symbol)
                    log(f"🔻 Closed {pos.symbol} at 19:59.")
                except Exception as e:
                    log(f"⚠️ Could not close {pos.symbol}: {e}")
        except Exception as e:
            log(f"⚠️ Error fetching positions at 19:59: {e}")


# ──────────────────────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────────────────────
@app.route('/')
def home():
    return jsonify({"status": "alive", "service": "ChrisBot1502"})


@app.route('/tv', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    log(f"📩 Webhook received: {data}")

    # Validate secret (trim whitespace)
    received_secret = str(data.get("secret", "")).strip()
    expected_secret = str(WEBHOOK_SECRET).strip()
    if received_secret != expected_secret:
        log(f"🚫 Unauthorized webhook attempt. Received: '{received_secret}' Expected: '{expected_secret}'")
        return jsonify({"error": "unauthorized"}), 401

    action = data.get("action", "").upper()
    ticker = data.get("ticker", "").upper()
    signal_close = float(data.get("signal_close", 0))
    qty = int(data.get("quantity", 100))
    source = data.get("source", "UNKNOWN")

    # ───── BUY ─────
    if action == "BUY":
        if loss_counter.get(ticker, 0) >= MAX_LOSSES_PER_TICKER:
            log(f"⚠️ Skipping BUY {ticker} — max losses reached.")
            return jsonify({"status": "skipped", "reason": "max losses"}), 200

        buffer = 0.03 if signal_close >= 1 else 0.003
        entry_price = round(signal_close + buffer, 4)
        log(f"🟢 BUY signal {ticker} entry={entry_price} qty={qty} src={source}")
        submit_limit_order(ticker, OrderSide.BUY, qty, entry_price)
        open_positions[ticker] = {"entry": entry_price, "qty": qty}

    # ───── SELL or STOP ─────
    elif action in ["SELL", "STOP"]:
        if ticker not in open_positions:
            log(f"⚠️ No open position for {ticker}. Ignoring SELL.")
            return jsonify({"status": "ignored"}), 200

        entry_data = open_positions[ticker]
        exit_price = float(signal_close)
        log(f"🔴 EXIT {ticker} close={exit_price}")
        submit_limit_order(ticker, OrderSide.SELL, entry_data["qty"], exit_price)
        del open_positions[ticker]
        loss_counter[ticker] = loss_counter.get(ticker, 0) + 1

    # ───── UNKNOWN ─────
    else:
        log(f"⚠️ Unknown action: {action}")
        return jsonify({"status": "error", "reason": "invalid action"}), 400

    close_all_positions_at_1959()
    return jsonify({"status": "ok"}), 200


# ──────────────────────────────────────────────────────────────
# SERVER START
# ──────────────────────────────────────────────────────────────
if __name__ == '__main__':
    log("🚀 Starting ChrisBot1502 webhook server ...")
    app.run(host="0.0.0.0", port=8080)































































