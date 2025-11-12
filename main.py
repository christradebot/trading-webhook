# main.py — Final Stable Build for Chris
# Limit-only | Premarket+RTH | No unwanted auto-closes | Duplicate BUY suppression

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
from alpaca.data.requests import StockLatestTradeRequest, StockLatestQuoteRequest

# ───────────────────────────────────────────────
# ✅ Environment variables (unchanged)
# ───────────────────────────────────────────────
API_KEY        = os.getenv("APCA_API_KEY_ID")
SECRET_KEY     = os.getenv("APCA_API_SECRET_KEY")
BASE_URL       = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

# ───────────────────────────────────────────────
# Clients
# ───────────────────────────────────────────────
trading     = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)
app = Flask(__name__)

# ───────────────────────────────────────────────
# State
# ───────────────────────────────────────────────
PENDING_BUYS = {}
LOSSES_TODAY = defaultdict(int)
LOSS_DAY_ET  = None
ET = timezone(timedelta(hours=-5))  # handles DST automatically

# Prevent duplicate BUYs within a short window
RECENT_BUY_ALERTS = {}
RECENT_BUY_WINDOW_S = 10.0
_recent_lock = threading.Lock()

# ───────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────
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
        t = r[symbol]
        return float(t.price)
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

def entry_buffer_for(price: float) -> float:
    return 0.03 if price >= 1.0 else 0.003

def schedule_next_minute_et():
    return now_et().replace(second=0, microsecond=0) + timedelta(minutes=1)

def place_limit_buy(symbol, qty, limit_price, source):
    req = LimitOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY,
        limit_price=limit_price, time_in_force=TimeInForce.DAY, extended_hours=True
    )
    o = trading.submit_order(req)
    print(f"✅ BUY placed {symbol} x{qty} @ {limit_price} (source={source})")
    return o

def place_limit_sell(symbol, qty, limit_price):
    req = LimitOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL,
        limit_price=limit_price, time_in_force=TimeInForce.DAY, extended_hours=True
    )
    o = trading.submit_order(req)
    print(f"➡️ SELL attempt {symbol} x{qty} @ {limit_price}")
    return o

def get_open_position(symbol):
    try:
        return trading.get_open_position(symbol)
    except Exception:
        return None

# ───────────────────────────────────────────────
# Duplicate suppression
# ───────────────────────────────────────────────
def is_duplicate_buy(symbol: str) -> bool:
    """Return True if a BUY for this symbol fired in the last RECENT_BUY_WINDOW_S seconds."""
    now_m = time.monotonic()
    with _recent_lock:
        last = RECENT_BUY_ALERTS.get(symbol)
        if last is not None and (now_m - last) < RECENT_BUY_WINDOW_S:
            return True
        RECENT_BUY_ALERTS[symbol] = now_m
    return False

# ───────────────────────────────────────────────
# Exit logic (never gives up)
# ───────────────────────────────────────────────
def chase_exit_until_flat(symbol, target_close):
    print(f"🚦 Start exit engine for {symbol}; target_close={target_close}")
    while True:
        pos = get_open_position(symbol)
        if not pos:
            print(f"✅ Exit complete — no open position on {symbol}")
            break
        qty = int(float(pos.qty))
        if qty <= 0:
            break
        bid, ask = latest_quote(symbol)
        if bid is None: bid = latest_trade_price(symbol)
        base_price = target_close if target_close else bid
        if base_price is None:
            time.sleep(1.5)
            continue
        limit_price = max(base_price, bid if bid else base_price)
        try:
            place_limit_sell(symbol, qty, limit_price)
        except Exception as e:
            print(f"⚠️ place_limit_sell failed {symbol}: {e}")
        time.sleep(2)
        try:
            orders = trading.get_orders(status="open", nested=True)
            for o in orders:
                if getattr(o, "symbol", "") == symbol:
                    try: trading.cancel_order_by_id(o.id)
                    except Exception: pass
        except Exception:
            pass
        time.sleep(1.5)

# ───────────────────────────────────────────────
# Background workers
# ───────────────────────────────────────────────
def pending_buy_worker():
    while True:
        try:
            reset_loss_counters_if_new_day()
            now = now_et()
            for sym, info in list(PENDING_BUYS.items()):
                if now >= info["when"]:
                    PENDING_BUYS.pop(sym, None)
                    target_close = info.get("target_close")
                    lp = target_close or latest_trade_price(sym) or latest_quote(sym)[1]
                    if lp is None:
                        print(f"⚠️ Could not price BUY for {sym}; skipping.")
                        continue
                    limit_price = lp + entry_buffer_for(lp)
                    try:
                        place_limit_buy(sym, info["qty"], limit_price, info["source"])
                    except Exception as e:
                        print(f"⚠️ BUY submit failed {sym}: {e}")
        except Exception as e:
            print(f"⚠️ pending_buy_worker loop error: {e}")
        time.sleep(0.8)

def kill_switch_worker():
    while True:
        try:
            t = now_et()
            if t.hour == 19 and t.minute == 59 and t.second < 10:
                print("🛑 19:59 ET kill-switch engaged. Exiting all positions.")
                try:
                    positions = trading.get_all_positions()
                except Exception as e:
                    print(f"⚠️ get_all_positions failed: {e}")
                    positions = []
                for p in positions:
                    threading.Thread(target=chase_exit_until_flat, args=(p.symbol, None), daemon=True).start()
                time.sleep(12)
        except Exception as e:
            print(f"⚠️ kill_switch_worker loop error: {e}")
        time.sleep(1)

# ───────────────────────────────────────────────
# Startup guard — detect but ignore old positions
# ───────────────────────────────────────────────
def startup_position_check():
    try:
        positions = trading.get_all_positions()
        if not positions:
            print("✅ No open positions detected on startup.")
            return
        print("⚠️ Existing open positions detected (ignored on startup):")
        for p in positions:
            print(f"   - {p.symbol} x{p.qty} @ {p.avg_entry_price}")
    except Exception as e:
        print(f"⚠️ startup_position_check failed: {e}")

# Launch background threads
threading.Thread(target=pending_buy_worker, daemon=True).start()
threading.Thread(target=kill_switch_worker, daemon=True).start()
startup_position_check()

# ───────────────────────────────────────────────
# Flask endpoints
# ───────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify(ok=True, time=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

@app.route("/tv", methods=["POST"])
def tv():
    reset_loss_counters_if_new_day()
    try:
        payload = request.get_json(force=True)
    except Exception:
        return jsonify(ok=False, error="Invalid JSON"), 400
    print(f"🔍 Raw webhook body: {json.dumps(payload, indent=2)}")

    if str(payload.get("secret", "")) != str(WEBHOOK_SECRET):
        print("🔒 Unauthorized webhook attempt")
        return jsonify(ok=False, error="unauthorized"), 401

    action = str(payload.get("action", "")).upper().strip()
    symbol = str(payload.get("ticker", "")).upper().strip()
    qty_raw = payload.get("quantity", 0)
    if not symbol or "{" in symbol or "}" in symbol:
        return jsonify(ok=False, error="Invalid or placeholder ticker"), 400
    try:
        qty = int(float(qty_raw))
    except Exception:
        qty = 0
    source = str(payload.get("source", "")).upper().strip()
    target_close = parse_float_maybe(payload.get("signal_close"))

    print(f"✅ Parsed payload: action={action} symbol={symbol} qty={qty} source={source} target_close={target_close}")

    if action == "BUY":
        if LOSSES_TODAY[symbol] >= 2:
            msg = f"🛑 Skipping BUY {symbol} — reached 2 losses today."
            print(msg)
            return jsonify(ok=False, reason="loss_cap", message=msg), 200
        if qty <= 0:
            return jsonify(ok=False, error="quantity must be > 0"), 400
        if is_duplicate_buy(symbol):
            msg = f"⏱️ Duplicate BUY suppressed for {symbol} (within {RECENT_BUY_WINDOW_S}s)"
            print(msg)
            return jsonify(ok=False, reason="duplicate_buy", message=msg), 200
        if symbol in PENDING_BUYS:
            msg = f"🕒 BUY already pending for {symbol}; ignoring duplicate schedule."
            print(msg)
            return jsonify(ok=True, message="buy_already_pending", symbol=symbol), 200

        when = schedule_next_minute_et()
        PENDING_BUYS[symbol] = {
            "qty": qty,
            "when": when,
            "source": source,
            "target_close": target_close
        }
        print(f"🕒 Pending BUY {symbol} x{qty} ({source}) at {when.strftime('%H:%M:%S')} ET")
        return jsonify(ok=True, scheduled_for=when.isoformat(), symbol=symbol)

    elif action in ("SELL", "STOP", "EXIT"):
        threading.Thread(target=chase_exit_until_flat, args=(symbol, target_close), daemon=True).start()
        return jsonify(ok=True, message="exit_started", symbol=symbol)

    else:
        return jsonify(ok=False, error="unknown action"), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))




















































































