# === main.py (with 20 EMA proximity filter) ===
# © Chris / Athena 2025

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST, APIError
import os, json, time

app = Flask(__name__)

#────────────────────────────────────────────
# Environment Config
#────────────────────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")

#────────────────────────────────────────────
# Parameters
#────────────────────────────────────────────
LIMIT_ONLY_MODE   = True
PRICE_TICK_BUFFER = 0.003
HARD_STOP_PCT     = 0.05
TRAIL_TP_PCT      = 0.10
EMA_PROXIMITY_PCT = 0.03      # within 3% of 20 EMA

#────────────────────────────────────────────
def log(msg): print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def get_nbbo(symbol):
    try:
        q = api.get_latest_quote(symbol)
        return float(q.bid_price or 0), float(q.ask_price or 0)
    except Exception as e:
        log(f"❌ NBBO error {symbol}: {e}")
        return 0, 0

def get_live_price(symbol):
    try:
        t = api.get_latest_trade(symbol)
        return float(t.price or 0)
    except Exception as e:
        log(f"❌ Price error {symbol}: {e}")
        return 0

def cancel_open_orders(symbol):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                api.cancel_order(o.id)
                log(f"🧹 Cancelled open {symbol}")
    except Exception as e:
        log(f"❌ Cancel error {symbol}: {e}")

#────────────────────────────────────────────
# Buy / Sell
#────────────────────────────────────────────
def submit_buy_limit(symbol, qty, ref_price):
    cancel_open_orders(symbol)
    bid, ask = get_nbbo(symbol)
    base = ref_price or ask or bid
    if base <= 0:
        log(f"⚠️ Invalid base price for {symbol}")
        return
    limit_price = round(base * (1 + PRICE_TICK_BUFFER), 4)
    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="limit",
            time_in_force="day",
            limit_price=str(limit_price),
            extended_hours=True
        )
        log(f"🟢 BUY {symbol} limit @{limit_price}")
    except Exception as e:
        log(f"❌ BUY submit error {symbol}: {e}")

def submit_sell_limit(symbol, qty, ref_price):
    cancel_open_orders(symbol)
    bid, ask = get_nbbo(symbol)
    base = bid or ref_price
    if base <= 0:
        log(f"⚠️ Invalid base price for {symbol}")
        return
    limit_price = round(base * (1 - PRICE_TICK_BUFFER), 4)
    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="sell",
            type="limit",
            time_in_force="day",
            limit_price=str(limit_price),
            extended_hours=True
        )
        log(f"🛑 SELL {symbol} limit @{limit_price}")
    except Exception as e:
        log(f"❌ SELL submit error {symbol}: {e}")

#────────────────────────────────────────────
# Synthetic Monitor
#────────────────────────────────────────────
def monitor_position(symbol, entry_price, qty):
    peak = entry_price
    while True:
        time.sleep(2)
        current = get_live_price(symbol)
        if not current: continue
        peak = max(peak, current)
        drawdown = (peak - current) / peak
        loss = (entry_price - current) / entry_price
        if drawdown >= TRAIL_TP_PCT:
            log(f"💰 TP hit {symbol} ({drawdown*100:.1f}%)")
            submit_sell_limit(symbol, qty, current)
            break
        if loss >= HARD_STOP_PCT:
            log(f"🛑 STOP hit {symbol} ({loss*100:.1f}%)")
            submit_sell_limit(symbol, qty, current)
            break

#────────────────────────────────────────────
# Webhook Endpoint
#────────────────────────────────────────────
@app.post("/tv")
def tv():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403

    event  = data.get("event", "").upper()
    symbol = data.get("ticker", "")
    qty    = float(data.get("qty", 100))
    price  = float(data.get("price", 0))
    ema20  = float(data.get("ema20", 0))  # now read from alert JSON

    log(f"🚀 TradingView Alert: {event} {symbol} price={price} ema20={ema20}")

    # ───── 20 EMA proximity check for BUYs ─────
    if event == "BUY":
        if ema20 > 0:
            dist = abs(price - ema20) / ema20
            if dist > EMA_PROXIMITY_PCT:
                log(f"⚠️ Skipping BUY {symbol}: too far from EMA20 ({dist*100:.2f}%)")
                return jsonify({"skipped": True}), 200
        submit_buy_limit(symbol, qty, price)
        monitor_position(symbol, price, qty)

    elif event in ["SELL", "EXIT"]:
        submit_sell_limit(symbol, qty, price)

    else:
        log(f"⚠️ Unknown event: {event}")

    return jsonify(status="ok"), 200

#────────────────────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, ema_check=EMA_PROXIMITY_PCT)

#────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)


















