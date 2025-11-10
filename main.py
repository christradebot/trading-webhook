# ===============================================================
# ChrisBot 1501 — Alpaca Trading Webhook Server
# Version: 2025-11-10b  (adds GET /tv + /health + route map log)
# ===============================================================

import os
from datetime import datetime
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

API_KEY       = os.getenv("APCA_API_KEY_ID")
SECRET_KEY    = os.getenv("APCA_API_SECRET_KEY")
BASE_URL      = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET= os.getenv("WEBHOOK_SECRET", "chrisbot1501")

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

trading = TradingClient(API_KEY, SECRET_KEY, paper=("paper" in BASE_URL))

app = Flask(__name__)

open_positions = {}
loss_counter = {}
MAX_LOSSES_PER_TICKER = 2

def now(): return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
def log(msg): print(f"[{now()}] {msg}", flush=True)

@app.before_first_request
def _show_routes():
    log("🔎 URL map:")
    for r in app.url_map.iter_rules():
        log(f"  {','.join(r.methods)}  {r}")

@app.get("/")
def root():
    return jsonify({"status":"alive","service":"ChrisBot1501","base_url":BASE_URL})

@app.get("/health")
def health():
    return jsonify({"ok":True,"time":now()})

# Helpful GET (so GET /tv won’t 404/405 during testing)
@app.get("/tv")
def tv_info():
    return jsonify({
        "ok": True,
        "message": "POST JSON to this endpoint to trade.",
        "required_fields": ["secret","action","ticker","quantity","signal_close","source"]
    })

def submit_limit(symbol, side, qty, price):
    try:
        req = LimitOrderRequest(
            symbol=symbol, qty=qty, side=side,
            limit_price=price, time_in_force=TimeInForce.DAY
        )
        order = trading.submit_order(req)
        log(f"✅ {side} {symbol} x{qty} @ {price}")
        return order
    except Exception as e:
        log(f"❌ submit_limit failed {symbol}: {e}")
        return None

def buffer_from_price(signal_close):
    return 0.03 if signal_close >= 1 else 0.003  # $0.03 or $0.003

@app.post("/tv")
def tv():
    data = request.get_json(force=True, silent=False)
    log(f"📩 {request.method} {request.path} {data}")

    # Auth
    if not data or data.get("secret") != WEBHOOK_SECRET:
        log("🚫 Unauthorized webhook attempt.")
        return jsonify({"error":"unauthorized"}), 401

    action = (data.get("action") or "").upper()
    symbol = (data.get("ticker") or "").upper()
    qty    = int(data.get("quantity") or 0)
    src    = data.get("source","UNKNOWN")
    try:
        sig_close = float(data.get("signal_close"))
    except Exception:
        return jsonify({"error":"signal_close must be numeric"}), 400

    if not symbol or qty <= 0:
        return jsonify({"error":"ticker/quantity invalid"}), 400

    # BUY
    if action == "BUY":
        if loss_counter.get(symbol, 0) >= MAX_LOSSES_PER_TICKER:
            log(f"⚠️ Skip BUY {symbol}: max losses reached.")
            return jsonify({"status":"skipped","reason":"max losses"}), 200

        price = round(sig_close + buffer_from_price(sig_close), 4)
        log(f"🕒 BUY plan {symbol}: entry={price} qty={qty} src={src}")
        submit_limit(symbol, OrderSide.BUY, qty, price)
        open_positions[symbol] = {"entry": price, "qty": qty}
        return jsonify({"status":"ok","submitted":"BUY","price":price}), 200

    # SELL / STOP
    if action in ("SELL","STOP"):
        pos = open_positions.get(symbol)
        if not pos:
            log(f"ℹ️ No open pos for {symbol}; still submitting SELL by request.")
            # Even without book-keeping, submit a sell (aggressive exit policy)
            price = round(sig_close, 4)
            submit_limit(symbol, OrderSide.SELL, qty, price)
            return jsonify({"status":"ok","submitted":"SELL","price":price}), 200

        price = round(sig_close, 4)
        log(f"🕒 SELL plan {symbol}: target={price} qty={pos['qty']} src={src}")
        submit_limit(symbol, OrderSide.SELL, pos["qty"], price)
        open_positions.pop(symbol, None)
        loss_counter[symbol] = loss_counter.get(symbol, 0) + 1
        return jsonify({"status":"ok","submitted":"SELL","price":price}), 200

    return jsonify({"error":"invalid action"}), 400

if __name__ == "__main__":
    log("🚀 Starting ChrisBot1501 …")
    app.run(host="0.0.0.0", port=8080)




































































