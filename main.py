# main.py
# Final build for Chris — limit-only, premarket+RTH, robust exits, loss caps, 19:59 ET kill-switch
# Now includes PnL + exit price logging for SELL exits

import os
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

# ───────────────────────────────────────────────────────────────
# ✅ Environment variables (keep names exactly as set in Railway)
# ───────────────────────────────────────────────────────────────
API_KEY        = os.getenv("APCA_API_KEY_ID")
SECRET_KEY     = os.getenv("APCA_API_SECRET_KEY")
BASE_URL       = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

# ───────────────────────────────────────────────────────────────
# ✅ Clients
# ───────────────────────────────────────────────────────────────
trading     = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ───────────────────────────────────────────────────────────────
# State
# ───────────────────────────────────────────────────────────────
app = Flask(__name__)
PENDING_BUYS = {}
LOSSES_TODAY = defaultdict(int)
LOSS_DAY_ET  = None

# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────
ET = timezone(timedelta(hours=-5))
def now_et(): return datetime.now(tz=ET)
def today_et_str(): return now_et().strftime("%Y-%m-%d")

def reset_loss_counters_if_new_day():
    global LOSS_DAY_ET
    d = today_et_str()
    if LOSS_DAY_ET != d:
        LOSSES_TODAY.clear()
        LOSS_DAY_ET = d
        print(f"🔄 Reset loss counters for new ET day: {d}")

def parse_float_maybe(x):
    try: return float(x)
    except Exception: return None

def latest_trade_price(symbol):
    try:
        r = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        return float(r[symbol].price)
    except Exception as e:
        print(f"⚠️ latest_trade_price error {symbol}: {e}")
        return None

def latest_quote(symbol):
    try:
        r = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
        q = r[symbol]
        return float(q.bid_price), float(q.ask_price)
    except Exception as e:
        print(f"⚠️ latest_quote error {symbol}: {e}")
        return None, None

def entry_buffer_for(price): return 0.03 if price >= 1.0 else 0.003
def schedule_next_minute_et(): return now_et().replace(second=0, microsecond=0) + timedelta(minutes=1)

def place_limit_buy(symbol, qty, limit_price, source):
    req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY,
                            limit_price=limit_price, time_in_force=TimeInForce.DAY, extended_hours=True)
    o = trading.submit_order(req)
    print(f"✅ BUY placed {symbol} x{qty} @ {limit_price} (source={source})")
    return o

def place_limit_sell(symbol, qty, limit_price):
    req = LimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.SELL,
                            limit_price=limit_price, time_in_force=TimeInForce.DAY, extended_hours=True)
    o = trading.submit_order(req)
    print(f"➡️ SELL attempt {symbol} x{qty} @ {limit_price}")
    return o

def get_open_position(symbol):
    try: return trading.get_open_position(symbol)
    except Exception: return None

def realized_loss_on_close(symbol, est_exit_price):
    pos = get_open_position(symbol)
    if not pos: return False
    try:
        avg = float(pos.avg_entry_price)
        if est_exit_price is None:
            bid, _ = latest_quote(symbol)
            est_exit_price = bid
        return est_exit_price is not None and est_exit_price < avg
    except Exception: return False

# ───────────────────────────────────────────────────────────────
# Aggressive exit engine (adds PnL logging)
# ───────────────────────────────────────────────────────────────
def chase_exit_until_flat(symbol, target_close):
    print(f"🚦 Start exit engine for {symbol}; target_close={target_close}")

    while True:
        pos = get_open_position(symbol)
        if not pos:
            print(f"✅ Exit complete — no open position on {symbol}")
            break

        qty = int(float(pos.qty))
        if qty <= 0:
            print(f"✅ Exit complete — qty is zero for {symbol}")
            break

        bid, ask = latest_quote(symbol)
        if bid is None and ask is None:
            last = latest_trade_price(symbol)
            bid = last

        base_price = target_close if target_close is not None else bid
        if base_price is None:
            time.sleep(1.5)
            continue

        limit_price = max(base_price, bid if bid else base_price)
        try:
            place_limit_sell(symbol, qty, limit_price)
        except Exception as e:
            print(f"⚠️ place_limit_sell failed {symbol}: {e}")
            time.sleep(1.5)
            continue

        time.sleep(2.0)

        try:
            orders = trading.get_orders(status="open", nested=True)
            for oo in orders:
                if getattr(oo, "symbol", "") == symbol:
                    try: trading.cancel_order_by_id(oo.id)
                    except Exception: pass
        except Exception: pass

    # Final PnL Logging
    try:
        last_price = latest_trade_price(symbol)
        pos_closed = getattr(pos, "avg_entry_price", None)
        if pos_closed and last_price:
            entry = float(pos_closed)
            pnl_val = last_price - entry
            pnl_pct = (pnl_val / entry) * 100
            sign = "📈" if pnl_val > 0 else "📉"
            print(f"{sign} Exit complete — {symbol} closed at {last_price:.2f} | PnL: {pnl_val:.2f} ({pnl_pct:.2f}%)")
    except Exception as e:
        print(f"⚠️ PnL log error for {symbol}: {e}")

    if realized_loss_on_close(symbol, target_close):
        LOSSES_TODAY[symbol] += 1
        print(f"📉 Recorded loss for {symbol}. Losses today: {LOSSES_TODAY[symbol]}")

# ───────────────────────────────────────────────────────────────
# Background workers
# ───────────────────────────────────────────────────────────────
def pending_buy_worker():
    while True:
        try:
            reset_loss_counters_if_new_day()
            now = now_et()
            to_place = [s for s, i in list(PENDING_BUYS.items()) if now >= i["when"]]
            for sym in to_place:
                info = PENDING_BUYS.pop(sym, None)
                if not info: continue

                lp = latest_trade_price(sym) or latest_quote(sym)[1]
                if lp is None:
                    print(f"⚠️ Could not price BUY for {sym}; skipping.")
                    continue

                limit_price = lp + entry_buffer_for(lp)
                try: place_limit_buy(sym, info["qty"], limit_price, info["source"])
                except Exception as e: print(f"⚠️ BUY submit failed {sym}: {e}")
        except Exception as e:
            print(f"⚠️ pending_buy_worker loop error: {e}")
        time.sleep(0.8)

def kill_switch_worker():
    while True:
        try:
            t = now_et()
            if t.hour == 19 and t.minute == 59 and t.second < 10:
                print("🛑 19:59 ET kill-switch engaged. Exiting all positions.")
                try: positions = trading.get_all_positions()
                except Exception as e:
                    print(f"⚠️ get_all_positions failed: {e}")
                    positions = []
                for p in positions:
                    sym, qty = p.symbol, int(float(p.qty))
                    threading.Thread(target=chase_exit_until_flat, args=(sym, None), daemon=True).start()
                time.sleep(12)
        except Exception as e:
            print(f"⚠️ kill_switch_worker loop error: {e}")
        time.sleep(1.0)

threading.Thread(target=pending_buy_worker, daemon=True).start()
threading.Thread(target=kill_switch_worker, daemon=True).start()

# ───────────────────────────────────────────────────────────────
# Flask endpoints
# ───────────────────────────────────────────────────────────────
@app.route("/health")
def health(): return jsonify(ok=True, time=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

@app.route("/tv", methods=["POST"])
def tv():
    reset_loss_counters_if_new_day()
    try: payload = request.get_json(force=True, silent=False)
    except Exception: return jsonify(ok=False, error="Invalid JSON"), 400

    print(f"🔍 Raw webhook body: {json.dumps(payload, indent=2)}")

    if str(payload.get("secret", "")) != str(WEBHOOK_SECRET):
        print("🔒 Unauthorized webhook attempt")
        return jsonify(ok=False, error="unauthorized"), 401

    action = str(payload.get("action", "")).upper().strip()
    symbol = str(payload.get("ticker", "")).upper().strip()
    qty_raw = payload.get("quantity", 0)

    if not symbol or "{" in symbol or "}" in symbol:
        return jsonify(ok=False, error="Invalid or placeholder ticker"), 400

    try: qty = int(float(qty_raw))
    except Exception: qty = 0

    source = str(payload.get("source", "")).upper().strip()
    target_close = parse_float_maybe(payload.get("signal_close"))
    print(f"✅ Parsed payload: action={action} symbol={symbol} qty={qty} source={source} target_close={target_close}")

    if action == "BUY":
        if LOSSES_TODAY[symbol] >= 2:
            msg = f"🛑 Skipping BUY {symbol} — reached 2 losses today."
            print(msg)
            return jsonify(ok=False, reason="loss_cap", message=msg), 200
        if qty <= 0: return jsonify(ok=False, error="quantity must be > 0"), 400
        when = schedule_next_minute_et()
        PENDING_BUYS[symbol] = {"qty": qty, "when": when, "source": source}
        print(f"🕒 Pending BUY for {symbol} x{qty} ({source}) at next bar {when.strftime('%H:%M:%S')} ET")
        return jsonify(ok=True, scheduled_for=when.isoformat(), symbol=symbol)

    elif action in ("SELL", "STOP", "EXIT"):
        threading.Thread(target=chase_exit_until_flat, args=(symbol, target_close), daemon=True).start()
        return jsonify(ok=True, message="exit_started", symbol=symbol)

    else:
        return jsonify(ok=False, error="unknown action"), 400

# ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))



















































































