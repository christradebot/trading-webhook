# main.py
# Stable build – preserves variable names, adds SELL ticker memory + detailed logs

import os, json, time, threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

# ───────────────────────────────
# ENV VARS (same names, untouched)
# ───────────────────────────────
API_KEY        = os.getenv("APCA_API_KEY_ID")
SECRET_KEY     = os.getenv("APCA_API_SECRET_KEY")
BASE_URL       = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API keys missing")

# ───────────────────────────────
# CLIENTS
# ───────────────────────────────
trading = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

app = Flask(__name__)

# ───────────────────────────────
# STATE
# ───────────────────────────────
ET = timezone(timedelta(hours=-5))
PENDING_BUYS = {}
LOSSES_TODAY = defaultdict(int)
LOSS_DAY_ET = None
LAST_SYMBOLS = {}  # remembers last BUY per source

# ───────────────────────────────
# HELPERS
# ───────────────────────────────
def now_et(): return datetime.now(tz=ET)
def today_et_str(): return now_et().strftime("%Y-%m-%d")

def reset_loss_counters_if_new_day():
    global LOSS_DAY_ET
    d = today_et_str()
    if LOSS_DAY_ET != d:
        LOSSES_TODAY.clear()
        LOSS_DAY_ET = d
        print(f"🔄 New ET day, loss counters reset: {d}")

def parse_float_maybe(x):
    try: return float(x)
    except: return None

def latest_trade_price(symbol):
    try:
        t = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))[symbol]
        return float(t.price)
    except Exception as e:
        print(f"⚠️ latest_trade_price {symbol}: {e}")
        return None

def latest_quote(symbol):
    try:
        q = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))[symbol]
        return float(q.bid_price), float(q.ask_price)
    except Exception as e:
        print(f"⚠️ latest_quote {symbol}: {e}")
        return None, None

def entry_buffer_for(price): return 0.03 if price >= 1 else 0.003

def place_limit_buy(symbol, qty, limit_price, source):
    o = trading.submit_order(LimitOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY,
        limit_price=limit_price, time_in_force=TimeInForce.DAY, extended_hours=True))
    print(f"✅ BUY placed {symbol} x{qty} @ {limit_price} (source={source})")
    LAST_SYMBOLS[source] = symbol  # remember for SELL
    return o

def place_limit_sell(symbol, qty, limit_price):
    o = trading.submit_order(LimitOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL,
        limit_price=limit_price, time_in_force=TimeInForce.DAY, extended_hours=True))
    print(f"➡️ SELL attempt {symbol} x{qty} @ {limit_price}")
    return o

def get_open_position(symbol):
    try: return trading.get_open_position(symbol)
    except: return None

# ───────────────────────────────
# EXIT ENGINE
# ───────────────────────────────
def chase_exit_until_flat(symbol, target_close):
    print(f"🚦 Exit engine start {symbol}, target_close={target_close}")
    while True:
        pos = get_open_position(symbol)
        if not pos: 
            print(f"✅ Exit complete — no position for {symbol}")
            break
        qty = int(float(pos.qty))
        if qty <= 0: 
            print(f"✅ Exit complete — qty=0 {symbol}")
            break
        bid, ask = latest_quote(symbol)
        if bid is None and ask is None: bid = latest_trade_price(symbol)
        base = target_close or bid
        if base is None: 
            time.sleep(1.5); continue
        limit_price = max(base, bid or base)
        try: place_limit_sell(symbol, qty, limit_price)
        except Exception as e: print(f"⚠️ sell fail {symbol}: {e}")
        time.sleep(2)
        try:
            for o in trading.get_orders(status="open", nested=True):
                if getattr(o, "symbol", "") == symbol:
                    trading.cancel_order_by_id(o.id)
        except: pass

# ───────────────────────────────
# BACKGROUND
# ───────────────────────────────
def pending_buy_worker():
    while True:
        try:
            reset_loss_counters_if_new_day()
            now = now_et()
            for sym, info in list(PENDING_BUYS.items()):
                if now >= info["when"]:
                    lp = latest_trade_price(sym) or latest_quote(sym)[1]
                    if lp is None: 
                        print(f"⚠️ No price for {sym}")
                        continue
                    price = lp + entry_buffer_for(lp)
                    try: place_limit_buy(sym, info["qty"], price, info["source"])
                    except Exception as e: print(f"⚠️ buy fail {sym}: {e}")
                    PENDING_BUYS.pop(sym, None)
        except Exception as e: print(f"⚠️ pending_buy loop: {e}")
        time.sleep(1)

threading.Thread(target=pending_buy_worker, daemon=True).start()

# ───────────────────────────────
# ROUTES
# ───────────────────────────────
@app.route("/health")
def health(): return jsonify(ok=True, time=datetime.utcnow().isoformat())

@app.route("/tv", methods=["POST"])
def tv():
    reset_loss_counters_if_new_day()
    try:
        payload = request.get_json(force=True)
    except Exception as e:
        print(f"❌ Bad JSON: {e}")
        return jsonify(ok=False, error="invalid_json"), 400

    print(f"\n📩 RAW: {json.dumps(payload, indent=2)}")

    # Secret
    if str(payload.get("secret", "")) != str(WEBHOOK_SECRET):
        print("🔒 Unauthorized secret"); return jsonify(ok=False), 401

    action  = str(payload.get("action", "")).upper().strip()
    symbol  = str(payload.get("ticker", "")).upper().strip()
    qty_raw = payload.get("quantity", 0)
    source  = str(payload.get("source", "")).upper().strip()
    target_close = parse_float_maybe(payload.get("signal_close"))

    # 🔧 ticker resolver
    if not symbol or "{" in symbol or "}" in symbol:
        fallback = LAST_SYMBOLS.get(source)
        if fallback:
            print(f"🧠 Resolved blank ticker via memory: {fallback} for source={source}")
            symbol = fallback
        else:
            print(f"⚠️ SELL skipped — no ticker and no memory for source={source}")
            return jsonify(ok=True, message="no_symbol_known"), 200

    try: qty = int(float(qty_raw))
    except: qty = 0

    print(f"✅ Parsed: {action} {symbol} x{qty} @close={target_close} src={source}")

    # BUY
    if action == "BUY":
        if LOSSES_TODAY[symbol] >= 2:
            print(f"🛑 Skipping BUY {symbol} (loss cap)")
            return jsonify(ok=False, reason="loss_cap"), 200
        when = now_et().replace(second=0, microsecond=0) + timedelta(minutes=1)
        PENDING_BUYS[symbol] = {"qty": qty, "when": when, "source": source}
        print(f"🕒 Pending BUY {symbol} scheduled {when.strftime('%H:%M:%S')} ET")
        return jsonify(ok=True, symbol=symbol, scheduled_for=when.isoformat())

    # SELL / EXIT
    elif action in ("SELL", "STOP", "EXIT"):
        pos = get_open_position(symbol)
        if not pos:
            print(f"⚠️ No open position for {symbol} — nothing to sell.")
            return jsonify(ok=True, message="no_open_position"), 200
        threading.Thread(target=chase_exit_until_flat, args=(symbol, target_close), daemon=True).start()
        return jsonify(ok=True, message="exit_started", symbol=symbol)

    else:
        return jsonify(ok=False, error="unknown_action"), 400

# ───────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))






















































































