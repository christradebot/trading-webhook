# main.py
# Final build for Chris — limit-only, premarket+RTH, robust exits, loss caps, 19:59 ET kill-switch
# Immediate BUY on alert (no next-bar wait) + one-bar (60s) fill window

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
# ✅ Environment variables (must match Railway names exactly)
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
# Config & State
# ───────────────────────────────────────────────────────────────
app = Flask(__name__)

# per-day loss cap (max 2 losses per ticker per ET trading day)
LOSSES_TODAY = defaultdict(int)
LOSS_DAY_ET  = None  # track date string "YYYY-MM-DD" in ET

# BUY monitoring: cancel unfilled buy after one bar (~60s)
BUY_FILL_WINDOW_SEC = 60

# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────
ET = timezone(timedelta(hours=-5))  # hosted env handles DST
def now_et() -> datetime:
    return datetime.now(tz=ET)

def today_et_str() -> str:
    return now_et().strftime("%Y-%m-%d")

def reset_loss_counters_if_new_day():
    global LOSS_DAY_ET
    d = today_et_str()
    if LOSS_DAY_ET != d:
        LOSSES_TODAY.clear()
        LOSS_DAY_ET = d
        print(f"🔄 Reset loss counters for new ET day: {d}")

def parse_float_maybe(x):
    try:
        return float(x)
    except Exception:
        return None

def latest_trade_price(symbol: str) -> float | None:
    try:
        r = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        t = r[symbol]
        return float(t.price)
    except Exception as e:
        print(f"⚠️ latest_trade_price error {symbol}: {e}")
        return None

def latest_quote(symbol: str):
    try:
        r = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
        q = r[symbol]
        return float(q.bid_price), float(q.ask_price)
    except Exception as e:
        print(f"⚠️ latest_quote error {symbol}: {e}")
        return None, None

def entry_buffer_for(price: float) -> float:
    # Above $1 → +$0.03; below $1 → +$0.003
    return 0.03 if price >= 1.0 else 0.003

def place_limit_buy(symbol: str, qty: int, limit_price: float, source: str):
    req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.BUY,
        limit_price=limit_price,
        time_in_force=TimeInForce.DAY,
        extended_hours=True
    )
    o = trading.submit_order(req)
    print(f"✅ BUY placed {symbol} x{qty} @ {limit_price} (source={source})")
    return o

def place_limit_sell(symbol: str, qty: int, limit_price: float):
    req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=OrderSide.SELL,
        limit_price=limit_price,
        time_in_force=TimeInForce.DAY,
        extended_hours=True
    )
    o = trading.submit_order(req)
    print(f"➡️  SELL attempt {symbol} x{qty} @ {limit_price}")
    return o

def get_open_position(symbol: str):
    try:
        return trading.get_open_position(symbol)
    except Exception:
        return None

def realized_loss_on_close(symbol: str, est_exit_price: float | None) -> bool:
    pos = get_open_position(symbol)
    if not pos:
        return False
    try:
        avg = float(pos.avg_entry_price)
        if est_exit_price is None:
            bid, _ = latest_quote(symbol)
            est_exit_price = bid
        return est_exit_price is not None and est_exit_price < avg
    except Exception:
        return False

# ───────────────────────────────────────────────────────────────
# Aggressive exit engine — will not give up until flat
# ───────────────────────────────────────────────────────────────
def chase_exit_until_flat(symbol: str, target_close: float | None):
    """
    Exit strategy:
      1) Try limit at max(target_close, current_bid) for longs.
      2) If not filled, cancel & chase to current_bid every ~2s.
      3) Keep going until position size is 0.
    """
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

        # Cancel unfilled SELLs to reprice
        try:
            orders = trading.get_orders(status="open", nested=True)
            for oo in orders:
                if getattr(oo, "symbol", "") == symbol and getattr(oo, "side", "").lower() == "sell":
                    try:
                        trading.cancel_order_by_id(oo.id)
                    except Exception:
                        pass
        except Exception:
            pass

    if realized_loss_on_close(symbol, target_close):
        LOSSES_TODAY[symbol] += 1
        print(f"📉 Recorded loss for {symbol}. Losses today: {LOSSES_TODAY[symbol]}")

# ───────────────────────────────────────────────────────────────
# BUY monitor — cancel if not filled within one bar
# ───────────────────────────────────────────────────────────────
def monitor_buy_for_one_bar(symbol: str, placed_at: float):
    """
    If there are still open BUY orders for `symbol` after BUY_FILL_WINDOW_SEC,
    cancel them (we don't chase buys endlessly).
    """
    deadline = placed_at + BUY_FILL_WINDOW_SEC
    while time.time() < deadline:
        time.sleep(1.0)
    try:
        orders = trading.get_orders(status="open", nested=True)
        for oo in orders:
            if getattr(oo, "symbol", "") == symbol and getattr(oo, "side", "").lower() == "buy":
                try:
                    trading.cancel_order_by_id(oo.id)
                    print(f"🛑 Canceled unfilled BUY for {symbol} after ~{BUY_FILL_WINDOW_SEC}s")
                except Exception:
                    pass
    except Exception:
        pass

# ───────────────────────────────────────────────────────────────
# Kill-switch @ 19:59 ET — exit everything
# ───────────────────────────────────────────────────────────────
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
                    sym = p.symbol
                    threading.Thread(target=chase_exit_until_flat, args=(sym, None), daemon=True).start()
                time.sleep(12)
        except Exception as e:
            print(f"⚠️ kill_switch_worker loop error: {e}")
        time.sleep(1.0)

threading.Thread(target=kill_switch_worker, daemon=True).start()

# ───────────────────────────────────────────────────────────────
# Flask endpoints
# ───────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify(ok=True, time=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

@app.route("/tv", methods=["POST"])
def tv():
    reset_loss_counters_if_new_day()

    # Force JSON (prevents 415)
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify(ok=False, error="Invalid JSON"), 400

    print(f"🔍 Raw webhook body: {json.dumps(payload, indent=2)}")

    # Secret (prevents 403)
    if str(payload.get("secret", "")) != str(WEBHOOK_SECRET):
        print("🔒 Unauthorized webhook attempt")
        return jsonify(ok=False, error="unauthorized"), 401

    action = str(payload.get("action", "")).upper().strip()
    symbol = str(payload.get("ticker", "")).upper().strip()
    qty_raw = payload.get("quantity", 0)
    source  = str(payload.get("source", "")).upper().strip()

    # Reject placeholders like {{TICKER}}
    if not symbol or "{" in symbol or "}" in symbol:
        return jsonify(ok=False, error="Invalid or placeholder ticker"), 400

    try:
        qty = int(float(qty_raw))
    except Exception:
        qty = 0

    target_close = parse_float_maybe(payload.get("signal_close"))

    print(f"✅ Parsed payload: action={action} symbol={symbol} qty={qty} source={source} close={target_close}")

    # Daily loss cap
    if action == "BUY":
        if LOSSES_TODAY[symbol] >= 2:
            msg = f"🛑 Skipping BUY {symbol} — reached 2 losses today."
            print(msg)
            return jsonify(ok=False, reason="loss_cap", message=msg), 200

        if qty <= 0:
            return jsonify(ok=False, error="quantity must be > 0"), 400

        # ── Immediate BUY at alert time (no next-bar wait)
        # Price ref: prefer latest trade, else ask; then apply fixed buffer
        ref = latest_trade_price(symbol)
        if ref is None:
            _, ask = latest_quote(symbol)
            ref = ask
        if ref is None:
            return jsonify(ok=False, error="no price reference available"), 200

        limit_price = ref + entry_buffer_for(ref)
        try:
            place_limit_buy(symbol, qty, limit_price, source)
            # monitor for one bar only; if not filled, cancel
            threading.Thread(target=monitor_buy_for_one_bar, args=(symbol, time.time()), daemon=True).start()
            return jsonify(ok=True, placed=True, symbol=symbol, price=limit_price)
        except Exception as e:
            print(f"⚠️ BUY submit failed {symbol}: {e}")
            return jsonify(ok=False, error="buy_failed", detail=str(e))), 200

    elif action in ("SELL", "STOP", "EXIT"):
        threading.Thread(target=chase_exit_until_flat, args=(symbol, target_close), daemon=True).start()
        return jsonify(ok=True, message="exit_started", symbol=symbol)

    else:
        return jsonify(ok=False, error="unknown action"), 400


# ───────────────────────────────────────────────────────────────
# WSGI entrypoint
# ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))


















































































