# main.py
# Chris + Athena — stable vars, limit-only, per-source SELL pairing, aggressive exits
# Added: one-open-position-at-a-time, 60s buy timeout (one-bar), duplicate buy guards

import os, json, time, threading
from datetime import datetime, timedelta, timezone
from collections import defaultdict, deque
from flask import Flask, request, jsonify

# ---- Alpaca SDK
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest

# ───────────────────────────────────────────────────────────────
# ✅ Environment variables — DO NOT CHANGE NAMES
# ───────────────────────────────────────────────────────────────
API_KEY        = os.getenv("APCA_API_KEY_ID")
SECRET_KEY     = os.getenv("APCA_API_SECRET_KEY")
BASE_URL       = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")  # <- your constant

if not API_KEY or not SECRET_KEY:
    raise ValueError("Alpaca API creds missing. Ensure APCA_API_KEY_ID/APCA_API_SECRET_KEY are set.")

# ───────────────────────────────────────────────────────────────
# Clients
# ───────────────────────────────────────────────────────────────
trading = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ───────────────────────────────────────────────────────────────
# App + State
# ───────────────────────────────────────────────────────────────
app = Flask(__name__)
ET = timezone(timedelta(hours=-5))

def now_et(): return datetime.now(tz=ET)

def _buf_for(price: float) -> float:
    # Absolute buffer, not percent: +$0.03 if >=$1 else +$0.003
    return 0.03 if price >= 1.0 else 0.003

def _to_float(v):
    try:
        return float(v)
    except Exception:
        return None

def latest_trade(symbol: str):
    try:
        r = data_client.get_stock_latest_trade(StockLatestTradeRequest(symbol_or_symbols=symbol))
        t = r[symbol]
        return float(t.price)
    except Exception as e:
        print(f"⚠️ latest_trade error {symbol}: {e}")
        return None

def latest_quote(symbol: str):
    try:
        r = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
        q = r[symbol]
        return float(q.bid_price), float(q.ask_price)
    except Exception as e:
        print(f"⚠️ latest_quote error {symbol}: {e}")
        return None, None

# Track most-recent buys per source as a stack: source -> deque of symbols (most recent at right)
active_by_source: dict[str, deque[str]] = defaultdict(deque)

def remember_buy(source: str, symbol: str):
    dq = active_by_source[source]
    try:
        dq.remove(symbol)
    except ValueError:
        pass
    dq.append(symbol)
    print(f"🧠 remember_buy: source={source} stack={list(dq)}")

def forget_symbol(source: str, symbol: str):
    dq = active_by_source.get(source)
    if not dq: return
    try:
        dq.remove(symbol)
        print(f"🧠 forget_symbol: removed {symbol} from {source}, remaining={list(dq)}")
    except ValueError:
        pass

def resolve_symbol_for_sell(source: str, payload_symbol: str | None) -> str | None:
    """
    If payload has a real ticker, use it. If it's blank or a placeholder,
    return the most recent symbol for this source from our stack.
    """
    s = (payload_symbol or "").strip().upper()
    if s and "{" not in s and "}" not in s:
        return s
    dq = active_by_source.get(source, deque())
    return dq[-1] if dq else None

def cancel_open_symbol_orders(symbol: str):
    try:
        orders = trading.get_orders(status="open", nested=True)
        for o in orders:
            if getattr(o, "symbol", "") == symbol:
                try:
                    trading.cancel_order_by_id(o.id)
                except Exception:
                    pass
    except Exception as e:
        print(f"⚠️ cancel_open_symbol_orders error {symbol}: {e}")

def any_open_orders() -> bool:
    try:
        orders = trading.get_orders(status="open", nested=True)
        return len(orders) > 0
    except Exception as e:
        print(f"⚠️ any_open_orders error: {e}")
        return False

def any_open_positions() -> bool:
    try:
        positions = trading.get_all_positions()
        return len(positions) > 0
    except Exception as e:
        print(f"⚠️ any_open_positions error: {e}")
        return False

def get_open_position(symbol: str):
    try:
        return trading.get_open_position(symbol)
    except Exception:
        return None

def place_limit_buy(symbol: str, qty: int, px: float, source: str):
    req = LimitOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.BUY,
        limit_price=px, time_in_force=TimeInForce.DAY, extended_hours=True
    )
    o = trading.submit_order(req)
    print(f"✅ BUY placed {symbol} x{qty} @ {px} (src={source}) id={o.id}")
    return o

def place_limit_sell(symbol: str, qty: int, px: float):
    req = LimitOrderRequest(
        symbol=symbol, qty=qty, side=OrderSide.SELL,
        limit_price=px, time_in_force=TimeInForce.DAY, extended_hours=True
    )
    o = trading.submit_order(req)
    print(f"➡️ SELL attempt {symbol} x{qty} @ {px} id={o.id}")
    return o

# ---- One-bar buy timeout support
PENDING_BUY_ORDERS: dict[str, dict] = {}  # symbol -> {"order_id": str, "deadline": datetime, "source": str}

def monitor_buy_timeout(symbol: str):
    """Cancel unfilled BUY after ~60s (one bar)."""
    info = PENDING_BUY_ORDERS.get(symbol)
    if not info:
        return
    order_id = info["order_id"]
    deadline = info["deadline"]
    source = info["source"]

    try:
        while datetime.now(tz=ET) < deadline:
            # If position opened, we're done; remember symbol for source pairing and clear pending
            pos = get_open_position(symbol)
            if pos and float(pos.qty) > 0:
                remember_buy(source, symbol)
                PENDING_BUY_ORDERS.pop(symbol, None)
                print(f"✅ BUY filled within one bar for {symbol}; pairing remembered.")
                return
            time.sleep(2.0)

        # Deadline passed -> cancel if still no position
        pos = get_open_position(symbol)
        if not (pos and float(pos.qty) > 0):
            try:
                trading.cancel_order_by_id(order_id)
                print(f"🛑 BUY timeout: canceled unfilled order for {symbol}")
            except Exception as e:
                print(f"⚠️ cancel after timeout failed {symbol}: {e}")
        PENDING_BUY_ORDERS.pop(symbol, None)
    except Exception as e:
        print(f"⚠️ monitor_buy_timeout error {symbol}: {e}")
        PENDING_BUY_ORDERS.pop(symbol, None)

def chase_exit_until_flat(symbol: str, target_close: float | None, source: str):
    """
    Keep repricing to current bid until qty == 0.
    Also cancels open buys first to avoid 'wash trade / existing buy limit' rejects.
    When flat, forget symbol from the source stack.
    """
    print(f"🚦 exit engine start symbol={symbol} target_close={target_close} source={source}")
    while True:
        pos = get_open_position(symbol)
        if not pos:
            print(f"✅ exit complete for {symbol}")
            break
        qty = int(float(pos.qty))
        if qty <= 0:
            print(f"✅ exit complete (qty==0) for {symbol}")
            break

        bid, _ask = latest_quote(symbol)
        last = latest_trade(symbol)
        ref = target_close if target_close is not None else (bid if bid is not None else last)
        if ref is None:
            time.sleep(1.0)
            continue
        limit_px = max(ref, bid) if bid is not None else ref

        # Avoid wash-trade rejects, and also cancel any pending BUY timeout watcher
        cancel_open_symbol_orders(symbol)
        PENDING_BUY_ORDERS.pop(symbol, None)

        try:
            place_limit_sell(symbol, qty, limit_px)
        except Exception as e:
            print(f"⚠️ place_limit_sell failed {symbol}: {e}")

        time.sleep(2.0)
        cancel_open_symbol_orders(symbol)

    # Remove from source stack after flat
    forget_symbol(source, symbol)

# ───────────────────────────────────────────────────────────────
# Flask endpoints
# ───────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify(ok=True, time=datetime.utcnow().isoformat())

@app.route("/tv", methods=["POST"])
def tv():
    # Be forgiving about headers: try JSON regardless
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify(ok=False, error="Invalid JSON"), 400

    print(f"📦 Raw: {json.dumps(payload, indent=2)}")

    # Auth
    if str(payload.get("secret", "")).strip() != str(WEBHOOK_SECRET):
        return jsonify(ok=False, error="unauthorized"), 401

    action = str(payload.get("action", "")).upper().strip()
    source = str(payload.get("source", "")).upper().strip() or "GENERIC"
    qty_raw = payload.get("quantity", 0)
    try:
        qty = int(float(qty_raw))
    except Exception:
        qty = 0

    # SELL target from alert if supplied
    target_close = _to_float(payload.get("signal_close"))

    # Resolve ticker (for BUY must be present; for SELL we can infer)
    raw_symbol = str(payload.get("ticker", "")).upper().strip()
    symbol = resolve_symbol_for_sell(source, raw_symbol) if action in ("SELL", "STOP", "EXIT") else raw_symbol

    # BUY logic
    if action == "BUY":
        if not symbol or "{" in symbol or "}" in symbol:
            return jsonify(ok=False, error="invalid ticker"), 400
        if qty <= 0:
            return jsonify(ok=False, error="quantity must be > 0"), 400

        # Guards: one open position at a time, and no stacking open orders
        if any_open_positions():
            return jsonify(ok=False, reason="holding", message="skip BUY — already holding a position"), 200
        if any_open_orders():
            return jsonify(ok=False, reason="open_order", message="skip BUY — open order exists"), 200
        if symbol in PENDING_BUY_ORDERS:
            return jsonify(ok=False, reason="pending_buy", message="skip BUY — pending buy timeout running"), 200

        # Price now + absolute buffer (immediate submit)
        px = latest_trade(symbol)
        if px is None:
            _bid, ask = latest_quote(symbol)
            px = ask
        if px is None:
            return jsonify(ok=False, error="no price available"), 400

        limit_px = round(px + _buf_for(px), 4)
        try:
            o = place_limit_buy(symbol, qty, limit_px, source)
        except Exception as e:
            return jsonify(ok=False, error=f"buy submit failed: {e}"), 200

        # Start one-bar (~60s) timeout monitor
        deadline = now_et() + timedelta(seconds=60)
        PENDING_BUY_ORDERS[symbol] = {"order_id": o.id, "deadline": deadline, "source": source}
        threading.Thread(target=monitor_buy_timeout, args=(symbol,), daemon=True).start()

        return jsonify(ok=True, symbol=symbol, submitted_at=now_et().isoformat(), limit=limit_px)

    # SELL / STOP / EXIT logic
    elif action in ("SELL", "STOP", "EXIT"):
        if not symbol:
            return jsonify(ok=False, error="no resolvable ticker for SELL (stack empty and payload missing)"), 400

        threading.Thread(
            target=chase_exit_until_flat,
            args=(symbol, target_close, source),
            daemon=True
        ).start()
        print(f"🧭 SELL pairing → source={source} symbol={symbol} target_close={target_close}")
        return jsonify(ok=True, message="exit_started", symbol=symbol)

    return jsonify(ok=False, error="unknown action"), 400


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))





















































































