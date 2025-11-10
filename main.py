import os
import json
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, Optional, Set

from flask import Flask, request, jsonify

# Alpaca v3 SDK (alpaca-py)
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# ------------------------
# ENV / CONFIG
# ------------------------
BASE_URL        = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
API_KEY         = os.getenv("APCA_API_KEY_ID")
API_SECRET      = os.getenv("APCA_API_SECRET_KEY")
WEBHOOK_SECRET  = os.getenv("WEBHOOK_SECRET")  # must equal alert JSON "secret"

# Guards (edit to taste)
MAX_OPEN_POSITIONS = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MAX_LOSSES_PER_TICKER = int(os.getenv("MAX_LOSSES_PER_TICKER", "2"))
COOLDOWN_SEC = int(os.getenv("LOSS_COOLDOWN_SEC", "300"))  # 5 min

if not all([API_KEY, API_SECRET, BASE_URL, WEBHOOK_SECRET]):
    print("❌ FATAL: Missing envs APCA_API_* or WEBHOOK_SECRET")
    raise SystemExit(1)

trading = TradingClient(API_KEY, API_SECRET, paper=True)
data    = StockHistoricalDataClient(API_KEY, API_SECRET)

app = Flask(__name__)

# ------------------------
# STATE
# ------------------------
class TickerState:
    def __init__(self):
        self.status: str = "flat"   # "flat" | "long"
        self.qty: int = 0
        self.open_price: float = 0.0
        self.losses: int = 0
        self.cooldown_until: datetime = datetime.fromtimestamp(0, tz=timezone.utc)
        self.pending_order_ids: Set[str] = set()

positions: Dict[str, TickerState] = {}
session_day: str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

def reset_if_new_day():
    global session_day, positions
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != session_day:
        print(f"🗓️ Session rollover: {session_day} → {today}. Resetting loss counters and state.")
        session_day = today
        # Keep statuses but reset losses/cooldowns
        for t, st in positions.items():
            st.losses = 0
            st.cooldown_until = datetime.fromtimestamp(0, tz=timezone.utc)

# ------------------------
# UTIL
# ------------------------
def buf_for_price(p: float) -> float:
    return 0.03 if p >= 1.0 else 0.003

def latest_trade_price(symbol: str) -> Optional[float]:
    try:
        tr = data.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        px = tr[symbol].price if symbol in tr and tr[symbol] else None
        return float(px) if px is not None else None
    except Exception as e:
        print(f"⚠️ latest_trade_price error {symbol}: {e}")
        return None

def place_limit_buy(symbol: str, qty: int, limit_price: float, extended: bool = True) -> Optional[str]:
    try:
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 4),
            extended_hours=extended
        )
        order = trading.submit_order(req)
        print(f"✅ BUY submitted {symbol} x{qty} @ {limit_price} (id={order.id})")
        return order.id
    except Exception as e:
        print(f"❌ BUY submit failed {symbol}: {e}")
        return None

def place_limit_sell(symbol: str, qty: int, limit_price: float, extended: bool = True) -> Optional[str]:
    try:
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 4),
            extended_hours=extended
        )
        order = trading.submit_order(req)
        print(f"✅ SELL submitted {symbol} x{qty} @ {limit_price} (id={order.id})")
        return order.id
    except Exception as e:
        print(f"❌ SELL submit failed {symbol}: {e}")
        return None

def close_position_aggressive(symbol: str, qty: int, signal_close: float):
    """
    SELL flow:
      1) Try limit at signal_close first
      2) If not immediately fillable, chase toward latest trade for a short window
    """
    # Step 1: first target = signal_close (minus small buffer so it hits)
    first_target = max(signal_close - buf_for_price(signal_close), 0.01)
    oid = place_limit_sell(symbol, qty, first_target)
    if oid is None:
        return False

    # Short chase loop (3s) – cancel/replace toward last price if we're far
    deadline = time.time() + 3.0
    while time.time() < deadline:
        time.sleep(0.4)
        live = latest_trade_price(symbol)
        if live is None:
            continue
        chase_price = max(live - buf_for_price(live), 0.01)
        if abs(chase_price - first_target) / max(0.01, first_target) > 0.002:  # ~0.2% drift
            try:
                trading.cancel_order_by_id(oid)
            except Exception:
                pass
            oid2 = place_limit_sell(symbol, qty, chase_price)
            if oid2:
                oid = oid2
                first_target = chase_price
    return True

def ensure_state(symbol: str) -> TickerState:
    if symbol not in positions:
        positions[symbol] = TickerState()
    return positions[symbol]

def open_positions_count() -> int:
    return sum(1 for s in positions.values() if s.status == "long")

# ------------------------
# WEBHOOK
# ------------------------
@app.route("/", methods=["GET"])
def root():
    return "OK", 200

@app.route("/tv", methods=["POST"])
def tv():
    reset_if_new_day()

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    secret = str(payload.get("secret", ""))
    if secret != WEBHOOK_SECRET:
        print("⛔ Unauthorized webhook attempt.")
        return jsonify({"ok": False, "error": "unauthorized"}), 403

    action = str(payload.get("action", "")).upper()
    symbol = str(payload.get("ticker", "")).upper()
    qty     = int(payload.get("quantity", "0") or 0)
    source  = str(payload.get("source", "")).upper()
    signal_close = float(payload.get("signal_close", "0") or 0)

    if not symbol or qty <= 0:
        return jsonify({"ok": False, "error": "missing ticker/quantity"}), 400

    st = ensure_state(symbol)

    print(f"📩 Webhook: {symbol} {action} qty={qty} source={source} sigClose={signal_close}")

    # ----- SELL (Smoothed HA)
    if action == "SELL":
        if st.status != "long" or st.qty <= 0:
            print(f"ℹ️ Ignoring SELL for {symbol}: not in position.")
            return jsonify({"ok": True, "note": "not_in_position"}), 200

        # Aggressive limit flow (target signal_close then chase)
        ok = close_position_aggressive(symbol, st.qty, signal_close or st.open_price)
        # Realized PnL estimate (based on last trade)
        sell_px = latest_trade_price(symbol) or signal_close or st.open_price
        pnl = (sell_px - st.open_price) * st.qty
        lost = pnl < 0

        if lost:
            st.losses += 1
            st.cooldown_until = datetime.now(timezone.utc) + timedelta(seconds=COOLDOWN_SEC)
            print(f"📉 LOSS recorded on {symbol}. losses={st.losses} cooldown_until={st.cooldown_until.isoformat()}")
        else:
            print(f"📈 WIN on {symbol}. (loss counter unchanged per your request)")

        st.status = "flat"
        st.qty = 0
        st.open_price = 0.0
        st.pending_order_ids.clear()

        return jsonify({"ok": ok, "pnl_est": round(pnl, 4), "losses": st.losses}), 200

    # ----- BUY (VBTS_TEMA_BUY or SMOOTH_HA_BUY)
    if action == "BUY":
        now = datetime.now(timezone.utc)

        # Blocks
        if st.status == "long":
            print(f"🚫 Block BUY {symbol}: already long.")
            return jsonify({"ok": False, "error": "already_long"}), 200

        if st.pending_order_ids:
            print(f"🚫 Block BUY {symbol}: pending order exists.")
            return jsonify({"ok": False, "error": "pending_order"}), 200

        if open_positions_count() >= MAX_OPEN_POSITIONS:
            print(f"🚫 Block BUY {symbol}: max open positions reached ({MAX_OPEN_POSITIONS}).")
            return jsonify({"ok": False, "error": "max_open_positions"}), 200

        if st.losses >= MAX_LOSSES_PER_TICKER:
            print(f"🚫 Block BUY {symbol}: losses limit hit ({st.losses}/{MAX_LOSSES_PER_TICKER}).")
            return jsonify({"ok": False, "error": "losses_limit"}), 200

        if now < st.cooldown_until:
            print(f"⏳ Block BUY {symbol}: cooldown until {st.cooldown_until.isoformat()}.")
            return jsonify({"ok": False, "error": "cooldown"}), 200

        # Entry limit = signal_close + buffer
        entry_base = signal_close if signal_close > 0 else (latest_trade_price(symbol) or 0)
        if entry_base <= 0:
            print(f"❌ No price basis available for {symbol}.")
            return jsonify({"ok": False, "error": "no_price"}), 200

        limit_price = round(entry_base + buf_for_price(entry_base), 4)

        oid = place_limit_buy(symbol, qty, limit_price, extended=True)
        if not oid:
            return jsonify({"ok": False, "error": "buy_submit_failed"}), 200

        st.pending_order_ids.add(oid)

        # Update state optimistically (we mark long immediately to avoid double-buys)
        st.status = "long"
        st.qty = qty
        st.open_price = limit_price

        return jsonify({"ok": True, "order_id": oid, "limit": limit_price}), 200

    return jsonify({"ok": False, "error": "unknown_action"}), 400


# ------------------------
# STARTUP
# ------------------------
if __name__ == "__main__":
    # Simple account ping
    try:
        acct = trading.get_account()
        print(f"✅ Connected — status: {acct.status.upper()}, equity: ${acct.equity}")
    except Exception as e:
        print(f"⚠️ Alpaca connection error: {e}")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))



























































