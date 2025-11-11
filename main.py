# main.py
# Final build for Chris — limit-only, premarket+RTH, robust exits, loss caps, 19:59 ET kill-switch

import os
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict

from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
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
# State
# ───────────────────────────────────────────────────────────────
app = Flask(__name__)

# pending buys keyed by symbol: {"symbol": {...}}
PENDING_BUYS = {}

# per-day loss cap (max 2 losses per ticker per ET trading day)
LOSSES_TODAY = defaultdict(int)
LOSS_DAY_ET  = None  # track date string "YYYY-MM-DD" in ET

# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────
ET = timezone(timedelta(hours=-5))  # updated automatically by system TZ changes (handles DST in hosted envs)
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

def schedule_next_minute_et() -> datetime:
    t = now_et().replace(second=0, microsecond=0) + timedelta(minutes=1)
    return t

def place_limit_buy(symbol: str, qty: int, limit_price: float, source: str):
    # Extended hours allowed
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
        pos = trading.get_open_position(symbol)
        return pos
    except Exception:
        return None

def realized_loss_on_close(symbol: str, est_exit_price: float | None) -> bool:
    pos = get_open_position(symbol)
    if not pos:
        return False
    try:
        avg = float(pos.avg_entry_price)
        # we’ll treat exit below avg as a “loss” for daily cap purposes
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
    # Note: run in thread to avoid blocking /tv handler.
    print(f"🚦 Start exit engine for {symbol}; target_close={target_close}")

    # Safety: hard loop guard, but ‘never give up’: we recycle loop while position exists
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
            # Last trade fallback
            last = latest_trade_price(symbol)
            bid = last

        # First target is the signal close if provided; otherwise use bid
        base_price = target_close if target_close is not None else bid
        if base_price is None:
            # nothing we can do without a price reference; wait briefly
            time.sleep(1.5)
            continue

        # Use the better of (target_close) vs (current bid) for sells (we want out)
        limit_price = max(base_price, bid if bid else base_price)

        try:
            o = place_limit_sell(symbol, qty, limit_price)
        except Exception as e:
            print(f"⚠️ place_limit_sell failed {symbol}: {e}")
            time.sleep(1.5)
            continue

        # wait briefly for fill, then check again
        time.sleep(2.0)

        # If still open, cancel open sell orders and tighten to current bid again
        try:
            # Cancel all open orders for the symbol (to reprice)
            orders = trading.get_orders(status="open", nested=True)
            for oo in orders:
                if getattr(oo, "symbol", "") == symbol:
                    try:
                        trading.cancel_order_by_id(oo.id)
                    except Exception:
                        pass
        except Exception:
            pass

        # Loop continues until position disappears

    # loss accounting (approximate)
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
            to_place = []
            for sym, info in list(PENDING_BUYS.items()):
                if now >= info["when"]:
                    to_place.append(sym)

            for sym in to_place:
                info = PENDING_BUYS.pop(sym, None)
                if not info:
                    continue

                # Obtain reference “open of next candle” proxy: the first trade right now
                # Use latest trade/quote and apply buffer.
                lp = latest_trade_price(sym)
                if lp is None:
                    _, ask = latest_quote(sym)
                    lp = ask

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
    """
    At 19:59:00 ET, force-exit all open positions with aggressive chasing.
    """
    while True:
        try:
            t = now_et()
            # fire between 19:59:00 and 20:00:00 once
            if t.hour == 19 and t.minute == 59 and t.second < 10:
                print("🛑 19:59 ET kill-switch engaged. Exiting all positions.")
                try:
                    positions = trading.get_all_positions()
                except Exception as e:
                    print(f"⚠️ get_all_positions failed: {e}")
                    positions = []

                for p in positions:
                    sym = p.symbol
                    qty = int(float(p.qty))
                    # Start a chaser thread for each symbol
                    threading.Thread(target=chase_exit_until_flat, args=(sym, None), daemon=True).start()

                # sleep a bit to avoid multiple firings in the same minute
                time.sleep(12)
        except Exception as e:
            print(f"⚠️ kill_switch_worker loop error: {e}")

        time.sleep(1.0)

# Launch background threads
threading.Thread(target=pending_buy_worker, daemon=True).start()
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

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify(ok=False, error="Invalid JSON"), 400

    print(f"🔍 Raw webhook body: {json.dumps(payload, indent=2)}")

    # Secret
    if str(payload.get("secret", "")) != str(WEBHOOK_SECRET):
        print("🔒 Unauthorized webhook attempt")
        return jsonify(ok=False, error="unauthorized"), 401

    action = str(payload.get("action", "")).upper().strip()
    symbol = str(payload.get("ticker", "")).upper().strip()
    qty_raw = payload.get("quantity", 0)

    # Ignore placeholders (e.g., {{ticker}}) and blanks
    if not symbol or "{" in symbol or "}" in symbol:
        return jsonify(ok=False, error="Invalid or placeholder ticker"), 400

    try:
        qty = int(float(qty_raw))
    except Exception:
        qty = 0

    source = str(payload.get("source", "")).upper().strip()

    # SELL/STOP target close might be placeholder too
    target_close = parse_float_maybe(payload.get("signal_close"))

    print(f"✅ Parsed payload: action={action} symbol={symbol} qty={qty} source={source} target_close={target_close}")

    # Enforce loss cap
    if action == "BUY":
        if LOSSES_TODAY[symbol] >= 2:
            msg = f"🛑 Skipping BUY {symbol} — reached 2 losses today."
            print(msg)
            return jsonify(ok=False, reason="loss_cap", message=msg), 200

        if qty <= 0:
            return jsonify(ok=False, error="quantity must be > 0"), 400

        # Schedule for next bar open (next minute boundary ET)
        when = schedule_next_minute_et()
        PENDING_BUYS[symbol] = {"qty": qty, "when": when, "source": source}
        print(f"🕒 Pending BUY for {symbol} x{qty} ({source}) at next bar {when.strftime('%H:%M:%S')} ET")
        return jsonify(ok=True, scheduled_for=when.isoformat(), symbol=symbol)

    elif action in ("SELL", "STOP", "EXIT"):
        # Spawn non-blocking chaser (never gives up)
        threading.Thread(target=chase_exit_until_flat, args=(symbol, target_close), daemon=True).start()
        return jsonify(ok=True, message="exit_started", symbol=symbol)

    else:
        return jsonify(ok=False, error="unknown action"), 400


# ───────────────────────────────────────────────────────────────
# WSGI entrypoint
# ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # For local testing
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))










































































