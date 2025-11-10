# main.py — Safe signal_close patch (prevents '{{close}}' crashes)
import os, json, time, threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

API_KEY = os.getenv("ALPACA_API_KEY", "").strip()
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "").strip()
BASE_URL = os.getenv("ALPACA_BASE_URL", "https://api.alpaca.markets").strip()
PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"
WEB_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501").strip()
APP_TZ = ZoneInfo("America/New_York")

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

app = Flask(__name__)

trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

DAILY_LOSSES = {}
LOSS_LIMIT = 2

def _today_key():
    return datetime.now(tz=APP_TZ).strftime("%Y-%m-%d")

def _inc_loss(ticker):
    day = _today_key()
    if day not in DAILY_LOSSES:
        DAILY_LOSSES[day] = {}
    DAILY_LOSSES[day][ticker] = DAILY_LOSSES[day].get(ticker, 0) + 1
    print(f"🧯 Loss counter → {ticker}: {DAILY_LOSSES[day][ticker]}/{LOSS_LIMIT}")

def _losses_left(ticker):
    return LOSS_LIMIT - DAILY_LOSSES.get(_today_key(), {}).get(ticker, 0)

def _latest_trade_price(symbol):
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol)
        lt = data_client.get_stock_latest_trade(req)
        return float(lt[symbol].price) if isinstance(lt, dict) else float(lt.price)
    except Exception as e:
        print(f"⚠️ Latest trade error for {symbol}: {e}")
        return 0.0

def _price_buffer(ref):
    return 0.03 if ref >= 1.0 else 0.003

def _normalize_symbol(s): return (s or "").upper().strip()

def _to_float_safe(val, default=None):
    if val is None: return default
    if isinstance(val, (int, float)): return float(val)
    s = str(val).strip()
    if "{{" in s and "}}" in s: return default
    try: return float(s)
    except: return default

# ───────────────────────────────────────────────
# Limit order functions
# ───────────────────────────────────────────────
def place_limit_buy(symbol, px, qty):
    try:
        order = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY,
                                  limit_price=px, time_in_force=TimeInForce.DAY,
                                  extended_hours=True)
        o = trading.submit_order(order)
        print(f"✅ BUY placed {symbol} x{qty} @ {px}")
        return o
    except Exception as e:
        print(f"❌ BUY failed {symbol}: {e}")

def place_limit_sell(symbol, px, qty):
    try:
        order = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                                  limit_price=px, time_in_force=TimeInForce.DAY,
                                  extended_hours=True)
        o = trading.submit_order(order)
        print(f"✅ SELL placed {symbol} x{qty} @ {px}")
        return o
    except Exception as e:
        print(f"❌ SELL failed {symbol}: {e}")

def close_position_aggressive_limit(symbol, prefer_px, side):
    try:
        pos = trading.get_open_position(symbol)
        qty = int(float(pos.qty))
    except: 
        print(f"ℹ️ No position to close for {symbol}")
        return

    for attempt in range(1, 6):
        live = _latest_trade_price(symbol) or 0.01
        px = prefer_px if (prefer_px and attempt == 1) else max(0.01, live - 0.01)
        print(f"⏳ Close attempt {attempt} for {symbol} @ {px}")
        if side.upper() == "SELL":
            if place_limit_sell(symbol, px, qty): return
        time.sleep(2)
    print(f"🚨 Could not close {symbol} after 5 attempts")

# ───────────────────────────────────────────────
# 19:59 ET close all
# ───────────────────────────────────────────────
def eod_closer_loop():
    while True:
        now = datetime.now(tz=APP_TZ)
        target = now.replace(hour=19, minute=59, second=0, microsecond=0)
        if now > target: target += timedelta(days=1)
        time.sleep((target - now).total_seconds())
        try:
            positions = trading.get_all_positions()
            for p in positions:
                sym = p.symbol; qty = int(float(p.qty))
                px = max(0.01, _latest_trade_price(sym) - 0.01)
                place_limit_sell(sym, px, qty)
            print("🧹 EOD 19:59 close triggered.")
        except Exception as e:
            print(f"⚠️ EOD closer error: {e}")

threading.Thread(target=eod_closer_loop, daemon=True).start()

# ───────────────────────────────────────────────
# Flask routes
# ───────────────────────────────────────────────
@app.route("/tv", methods=["POST"])
def tv():
    payload = request.get_json(force=True, silent=True) or {}
    print(f"🔍 Raw: {json.dumps(payload)}")

    if str(payload.get("secret","")).strip() != WEB_SECRET:
        return jsonify({"ok":False,"error":"unauthorized"}),401

    action = str(payload.get("action","")).upper()
    symbol = _normalize_symbol(payload.get("ticker"))
    qty = int(float(payload.get("quantity",100)))
    source = str(payload.get("source","")).upper()

    if not symbol:
        return jsonify({"ok":False,"error":"no_symbol"}),400

    # safe close parse
    raw_close = payload.get("signal_close",0.0)
    try:
        target_close = float(raw_close)
    except (ValueError,TypeError):
        print(f"⚠️ signal_close not numeric ('{raw_close}')")
        target_close = None

    ref = target_close if target_close else _latest_trade_price(symbol)
    buf = _price_buffer(ref)
    losses_left = _losses_left(symbol)

    if action=="BUY":
        if losses_left<=0:
            print(f"🛑 Loss cap hit for {symbol}")
            return jsonify({"ok":False,"ignored":"loss_cap"}),200
        if not ref:
            return jsonify({"ok":False,"error":"no_ref"}),200
        px = round(ref+buf,4)
        print(f"🕒 BUY {symbol} @ {px} ({source})")
        place_limit_buy(symbol,px,qty)
        return jsonify({"ok":True,"placed":"buy","symbol":symbol,"limit":px}),200

    if action=="SELL":
        prefer = target_close if target_close else None
        print(f"🕒 SELL {symbol} ({source}) prefer={prefer}")
        try:
            pos = trading.get_open_position(symbol)
            avg = float(pos.avg_entry_price)
        except: avg=None
        close_position_aggressive_limit(symbol,prefer,"SELL")
        try:
            time.sleep(0.5)
            trading.get_open_position(symbol)
            still=True
        except: still=False
        if not still and avg:
            exit_px = prefer or _latest_trade_price(symbol)
            if exit_px and exit_px<avg: _inc_loss(symbol)
        return jsonify({"ok":True,"placed":"sell","symbol":symbol}),200

    return jsonify({"ok":False,"error":"unknown_action"}),200

@app.route("/health")
def health(): return jsonify({"ok":True,"paper":PAPER})

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",8080)),debug=False)









































































