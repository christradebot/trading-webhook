# main.py
# Chris final — LIMIT ONLY, instant BUY, robust SELL-chase, stable vars, rich logs

import os
import json
import time
import threading
from datetime import datetime, timedelta, timezone
from collections import deque

from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

# ───────────────────────────────────────────────────────────────
# ✅ Environment variables (names MUST match Railway)
# ───────────────────────────────────────────────────────────────
API_KEY        = os.getenv("APCA_API_KEY_ID")
SECRET_KEY     = os.getenv("APCA_API_SECRET_KEY")
BASE_URL       = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")  # case-sensitive

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

# ───────────────────────────────────────────────────────────────
# ✅ Clients
# ───────────────────────────────────────────────────────────────
trading     = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ───────────────────────────────────────────────────────────────
# App/state
# ───────────────────────────────────────────────────────────────
app = Flask(__name__)

# Track most recent BUY attempts (for SELL fallback if ticker missing)
# deque of dicts: {"symbol": str, "ts": datetime, "source": str}
LAST_BUYS = deque(maxlen=50)

# ───────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────
ET = timezone(timedelta(hours=-5))
def now_et() -> datetime:
    return datetime.now(tz=ET)

def parse_float(val):
    try:
        return float(val)
    except Exception:
        return None

def looks_like_placeholder(s: str) -> bool:
    if not s:
        return True
    s = s.strip().upper()
    return ("{" in s) or ("}" in s) or s in ("TICKER", "{{TICKER}}")

def entry_buffer_for(price: float) -> float:
    # Above $1 → +$0.03; below $1 → +$0.003
    return 0.03 if price >= 1.0 else 0.003

def latest_trade_price(symbol: str) -> float | None:
    try:
        r = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        t = r[symbol]
        return float(t.price)
    except Exception as e:
        app.logger.error(f"⚠️ latest_trade_price error {symbol}: {e}")
        return None

def latest_quote(symbol: str):
    try:
        r = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
        q = r[symbol]
        bid = parse_float(q.bid_price)
        ask = parse_float(q.ask_price)
        return bid, ask
    except Exception as e:
        app.logger.error(f"⚠️ latest_quote error {symbol}: {e}")
        return None, None

def get_open_position(symbol: str):
    try:
        return trading.get_open_position(symbol)
    except Exception:
        return None

def any_open_position_exists() -> bool:
    # If you ever want "only one position at a time total", flip this on and check len > 0.
    try:
        positions = trading.get_all_positions()
        return len(positions) > 0
    except Exception:
        return False

def open_positions_by_symbol() -> dict:
    out = {}
    try:
        for p in trading.get_all_positions():
            out[p.symbol] = p
    except Exception:
        pass
    return out

def there_is_open_buy_order_for(symbol: str) -> bool:
    try:
        for o in trading.get_orders(status="open"):
            if getattr(o, "symbol", "") == symbol and getattr(o, "side", "").lower() == "buy":
                return True
    except Exception:
        pass
    return False

def append_last_buy(symbol: str, source: str):
    LAST_BUYS.appendleft({"symbol": symbol, "ts": now_et(), "source": source})

def resolve_symbol_for_sell(incoming_symbol: str | None, source: str | None) -> str | None:
    """
    If SELL ticker is missing/placeholder:
      1) If exactly one open position exists -> use that symbol
      2) Else pick most-recent LAST_BUYS symbol that is still open
      3) Else return None (cannot resolve)
    """
    sym = (incoming_symbol or "").upper().strip()
    if sym and not looks_like_placeholder(sym):
        return sym

    positions = open_positions_by_symbol()
    if len(positions) == 1:
        sym_only = list(positions.keys())[0]
        app.logger.info(f"🧭 SELL fallback → only open position: {sym_only}")
        return sym_only

    # Try most-recent last-buys that are still open
    if positions:
        for entry in LAST_BUYS:
            s = entry.get("symbol", "")
            if s in positions:
                app.logger.info(f"🧭 SELL fallback → most recent open from LAST_BUYS: {s}")
                return s

    app.logger.warning("🧭 SELL fallback failed — unable to resolve symbol from payload/open positions.")
    return None

# ───────────────────────────────────────────────────────────────
# Order placement
# ───────────────────────────────────────────────────────────────
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
    app.logger.info(f"✅ BUY placed {symbol} x{qty} @ {limit_price:.4f} (source={source})")
    append_last_buy(symbol, source)
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
    app.logger.info(f"➡️ SELL attempt {symbol} x{qty} @ {limit_price:.4f}")
    return o

# ───────────────────────────────────────────────────────────────
# Exit engine — limit-only, chases until flat
# ───────────────────────────────────────────────────────────────
def chase_exit_until_flat(symbol: str, target_close: float | None):
    app.logger.info(f"🚦 Exit engine start: {symbol} target_close={target_close}")
    while True:
        pos = get_open_position(symbol)
        if not pos:
            app.logger.info(f"✅ Exit complete — no open position on {symbol}")
            break

        try:
            qty = int(float(pos.qty))
        except Exception:
            qty = 0

        if qty <= 0:
            app.logger.info(f"✅ Exit complete — qty 0 for {symbol}")
            break

        bid, ask = latest_quote(symbol)
        if bid is None and ask is None:
            last = latest_trade_price(symbol)
            bid = last

        # For sells, we prefer to be hit; choose max(target_close, current bid)
        base = target_close if (target_close is not None and target_close > 0) else bid
        if base is None:
            time.sleep(1.0)
            continue

        limit_price = max(base, bid if bid else base)

        try:
            place_limit_sell(symbol, qty, limit_price)
        except Exception as e:
            app.logger.error(f"⚠️ SELL submit failed {symbol}: {e}")
            time.sleep(1.2)
            continue

        # Give fills a moment
        time.sleep(1.8)

        # Reprice: cancel open sells for this symbol so we can tighten next loop
        try:
            for o in trading.get_orders(status="open", nested=True):
                if getattr(o, "symbol", "") == symbol and getattr(o, "side", "").lower() == "sell":
                    try:
                        trading.cancel_order_by_id(o.id)
                    except Exception:
                        pass
        except Exception:
            pass

# ───────────────────────────────────────────────────────────────
# Flask endpoints
# ───────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify(ok=True, time=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))

@app.route("/tv", methods=["POST"])
def tv():
    # Accept JSON even if TradingView doesn’t set Content-Type
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception as e:
        app.logger.error(f"❌ 400 Invalid JSON: {e}")
        return jsonify(ok=False, error="invalid_json"), 400

    app.logger.info(f"🔍 Raw webhook: {json.dumps(payload, indent=2)}")

    # Secret (case-sensitive)
    if str(payload.get("secret", "")) != str(WEBHOOK_SECRET):
        app.logger.error("🔒 403 Wrong secret")
        return jsonify(ok=False, error="unauthorized"), 403

    action = str(payload.get("action", "")).upper().strip()
    source = str(payload.get("source", "")).upper().strip()
    raw_symbol = str(payload.get("ticker", "")).upper().strip()
    qty_raw = payload.get("quantity", 0)
    target_close = parse_float(payload.get("signal_close"))

    # Normalize qty
    try:
        qty = int(float(qty_raw))
    except Exception:
        qty = 0

    # Resolve symbol (BUY uses payload; SELL can fallback if placeholder)
    if action == "SELL":
        symbol = resolve_symbol_for_sell(raw_symbol, source)
    else:
        symbol = raw_symbol if not looks_like_placeholder(raw_symbol) else ""

    app.logger.info(
        f"✅ Parsed: action={action or '?'} symbol={symbol or raw_symbol or '?'} "
        f"qty={qty} src={source or '?'} close={target_close}"
    )

    if action == "BUY":
        if not symbol:
            app.logger.error("⚠️ BUY rejected — missing/placeholder ticker")
            return jsonify(ok=False, error="missing_ticker"), 400
        if qty <= 0:
            app.logger.error("⚠️ BUY rejected — quantity must be > 0")
            return jsonify(ok=False, error="bad_quantity"), 400

        # Prevent double-buys: if we already hold symbol or have an open BUY order, skip
        if get_open_position(symbol):
            app.logger.info(f"🛑 BUY skipped — already in position for {symbol}")
            return jsonify(ok=True, reason="already_in_position", symbol=symbol)
        if there_is_open_buy_order_for(symbol):
            app.logger.info(f"🛑 BUY skipped — open BUY order already exists for {symbol}")
            return jsonify(ok=True, reason="open_buy_exists", symbol=symbol)

        # Price reference = latest trade (fallback ask)
        last = latest_trade_price(symbol)
        if last is None:
            _, ask = latest_quote(symbol)
            last = ask
        if last is None:
            app.logger.error(f"⚠️ BUY aborted — no price available for {symbol}")
            return jsonify(ok=False, error="no_price"), 200  # soft fail to avoid retries

        limit_price = last + entry_buffer_for(last)
        try:
            place_limit_buy(symbol, qty, limit_price, source or "TV")
            return jsonify(ok=True, placed=True, symbol=symbol, limit=round(limit_price, 6))
        except Exception as e:
            app.logger.error(f"⚠️ place_limit_buy failed {symbol}: {e}")
            return jsonify(ok=False, error="buy_failed", detail=str(e)), 200

    elif action in ("SELL", "STOP", "EXIT"):
        if not symbol:
            # Nothing we can do; log but don't 500 your session
            app.logger.error("⚠️ SELL received without resolvable ticker; no-op")
            return jsonify(ok=True, noop=True, reason="missing_ticker"), 200

        # Spawn non-blocking exit engine (limit-only, chases until flat)
        threading.Thread(target=chase_exit_until_flat, args=(symbol, target_close), daemon=True).start()
        return jsonify(ok=True, exit_started=True, symbol=symbol, target_close=target_close), 200

    else:
        app.logger.error(f"⚠️ Unknown action: {action}")
        return jsonify(ok=False, error="unknown_action"), 400

# ───────────────────────────────────────────────────────────────
# Entrypoint
# ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))






















































































