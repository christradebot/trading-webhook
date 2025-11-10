import os
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set

from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# ───────────────────────────────────────────────
# CONFIG
# ───────────────────────────────────────────────
API_KEY        = os.getenv("ALPACA_API_KEY")
SECRET_KEY     = os.getenv("ALPACA_SECRET_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "chrisbot1501")
PAPER          = True

MAX_OPEN_POSITIONS     = 3
MAX_LOSSES_PER_TICKER  = 2
COOLDOWN_SEC           = 300      # 5 minutes
AUTO_EXIT_HOUR         = 19       # 19:59 UTC
AUTO_EXIT_MINUTE       = 59

trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data    = StockHistoricalDataClient(API_KEY, SECRET_KEY)

app = Flask(__name__)

# ───────────────────────────────────────────────
# STATE
# ───────────────────────────────────────────────
class TickerState:
    def __init__(self):
        self.status = "flat"
        self.qty = 0
        self.open_price = 0.0
        self.losses = 0
        self.cooldown_until = datetime.fromtimestamp(0, tz=timezone.utc)
        self.pending_order_ids: Set[str] = set()

positions: Dict[str, TickerState] = {}
session_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# ───────────────────────────────────────────────
# HELPERS
# ───────────────────────────────────────────────
def buf_for_price(p: float) -> float:
    return 0.03 if p >= 1.0 else 0.003

def latest_trade_price(symbol: str) -> Optional[float]:
    try:
        t = data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        return float(t[symbol].price)
    except Exception as e:
        print(f"⚠️ price fetch failed {symbol}: {e}")
        return None

def limit_order(symbol: str, qty: int, side: str, limit_price: float):
    try:
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            limit_price=round(limit_price, 4),
            time_in_force=TimeInForce.DAY,
            extended_hours=True
        )
        o = trading.submit_order(req)
        print(f"✅ {side} {symbol} x{qty} @ {limit_price}")
        return o.id
    except Exception as e:
        print(f"❌ {side} failed {symbol}: {e}")
        return None

def chase_sell(symbol: str, qty: int, signal_close: float):
    """Try limit at signal_close, chase if needed."""
    base = max(signal_close - buf_for_price(signal_close), 0.01)
    oid = limit_order(symbol, qty, "SELL", base)
    if not oid:
        return
    for _ in range(5):
        time.sleep(0.4)
        live = latest_trade_price(symbol)
        if not live:
            continue
        chase = max(live - buf_for_price(live), 0.01)
        try:
            trading.cancel_order_by_id(oid)
        except Exception:
            pass
        oid = limit_order(symbol, qty, "SELL", chase)
    return True

def ensure(symbol: str) -> TickerState:
    if symbol not in positions:
        positions[symbol] = TickerState()
    return positions[symbol]

def open_positions() -> int:
    return sum(1 for s in positions.values() if s.status == "long")

def reset_daily():
    global session_day
    now_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if now_day != session_day:
        print(f"🗓️ Resetting session {session_day} → {now_day}")
        for s in positions.values():
            s.losses = 0
            s.cooldown_until = datetime.fromtimestamp(0, tz=timezone.utc)
        session_day = now_day

# ───────────────────────────────────────────────
# AUTO EXIT LOOP (19:59 UTC)
# ───────────────────────────────────────────────
def auto_exit_loop():
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == AUTO_EXIT_HOUR and now.minute == AUTO_EXIT_MINUTE:
            print(f"⏰ Auto-exit trigger {now}")
            for sym, st in list(positions.items()):
                if st.status == "long" and st.qty > 0:
                    px = latest_trade_price(sym) or st.open_price
                    print(f"⚙️ Auto-exit {sym} qty={st.qty} @ {px}")
                    chase_sell(sym, st.qty, px)
                    st.status = "flat"
                    st.qty = 0
                    st.open_price = 0
                    st.pending_order_ids.clear()
            time.sleep(65)
        time.sleep(20)

threading.Thread(target=auto_exit_loop, daemon=True).start()

# ───────────────────────────────────────────────
# FLASK ROUTES
# ───────────────────────────────────────────────
@app.route("/")
def root():
    return "✅ Bot live", 200

@app.route("/tv", methods=["POST"])
def tv():
    reset_daily()
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "bad_json"}), 400

    if data.get("secret") != WEBHOOK_SECRET:
        print("⛔ Unauthorized webhook")
        return jsonify({"error": "unauthorized"}), 403

    action = data.get("action", "").upper()
    sym = data.get("ticker", "").upper()
    qty = int(float(data.get("quantity", 0)))
    sig_close = float(data.get("signal_close", 0))
    src = data.get("source", "")

    if not sym or qty <= 0:
        return jsonify({"error": "bad_fields"}), 400

    s = ensure(sym)
    now = datetime.now(timezone.utc)

    print(f"📩 {sym} {action} {qty} src={src} close={sig_close}")

    # ---- SELL ----
    if action == "SELL":
        if s.status != "long":
            print(f"ℹ️ Skip SELL {sym}: not long.")
            return jsonify({"ok": True, "note": "flat"}), 200
        chase_sell(sym, s.qty, sig_close or s.open_price)
        s.status = "flat"
        s.qty = 0
        s.open_price = 0
        s.pending_order_ids.clear()
        print(f"✅ Exited {sym} via SELL.")
        return jsonify({"ok": True}), 200

    # ---- BUY ----
    if action == "BUY":
        if s.status == "long":
            return jsonify({"error": "already_long"}), 200
        if open_positions() >= MAX_OPEN_POSITIONS:
            return jsonify({"error": "too_many_positions"}), 200
        if s.losses >= MAX_LOSSES_PER_TICKER:
            return jsonify({"error": "loss_limit"}), 200
        if now < s.cooldown_until:
            return jsonify({"error": "cooldown"}), 200

        base = sig_close if sig_close > 0 else latest_trade_price(sym)
        if not base:
            return jsonify({"error": "no_price"}), 200

        limit = round(base + buf_for_price(base), 4)
        oid = limit_order(sym, qty, "BUY", limit)
        if oid:
            s.status = "long"
            s.qty = qty
            s.open_price = limit
            s.pending_order_ids.add(oid)
            print(f"📈 Entered {sym} @ {limit}")
        return jsonify({"ok": True, "limit": limit}), 200

    return jsonify({"error": "unknown_action"}), 400

# ───────────────────────────────────────────────
# STARTUP
# ───────────────────────────────────────────────
if __name__ == "__main__":
    try:
        acct = trading.get_account()
        print(f"✅ Connected — {acct.status.upper()}  Equity: ${acct.equity}")
    except Exception as e:
        print(f"⚠️ Connection error: {e}")
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)




























































