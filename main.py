# === main.py ===
# © Chris / Athena 2025
# HMA + MaMA Execution Bot — with Add-25-Share Scaling Logic

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST, TimeFrame, APIError
import os, json, time, math

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# ENV / API
# ──────────────────────────────────────────────────────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")

# ──────────────────────────────────────────────────────────────────────────────
# PARAMETERS
# ──────────────────────────────────────────────────────────────────────────────
HMA_LEN              = 14
ENTRY_BUFFER_PCT     = float(os.environ.get("ENTRY_BUFFER_PCT", "0.02"))  # 2%
EXIT_BUFFER_PCT      = float(os.environ.get("EXIT_BUFFER_PCT",  "0.02"))  # 2%
CHASE_REPRICES       = int(os.environ.get("CHASE_REPRICES", "8"))
CHASE_SLEEP_SEC      = float(os.environ.get("CHASE_SLEEP_SEC", "1.5"))
AGG_EXIT_EXTRA_PCT   = float(os.environ.get("AGG_EXIT_EXTRA_PCT", "0.01"))
ADD_SHARES_QTY       = int(os.environ.get("ADD_SHARES_QTY", "25"))
LOG_PREFIX           = "[HMA]"

# ──────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────────────────────────────────────
def log(msg): print(f"{LOG_PREFIX} {time.strftime('%H:%M:%S')}  {msg}", flush=True)

def round_price(p: float) -> float:
    if p < 1:   return round(p, 4)
    if p < 10:  return round(p, 3)
    return round(p, 2)

def get_nbbo(symbol):
    try:
        q = api.get_latest_quote(symbol)
        bid = float(q.bid_price) if q and q.bid_price else None
        ask = float(q.ask_price) if q and q.ask_price else None
        return bid, ask
    except Exception as e:
        log(f"NBBO error {symbol}: {e}")
        return None, None

def get_last(symbol):
    try:
        t = api.get_latest_trade(symbol)
        return float(t.price) if t and t.price else None
    except Exception as e:
        log(f"Last trade error {symbol}: {e}")
        return None

def get_position_qty(symbol) -> float:
    try:
        pos = api.get_position(symbol)
        return float(pos.qty)
    except APIError:
        return 0.0
    except Exception as e:
        log(f"Position error {symbol}: {e}")
        return 0.0

def cancel_open_orders(symbol):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                api.cancel_order(o.id)
                log(f"🧹 Cancelled open order: {symbol}")
    except Exception as e:
        log(f"Cancel error {symbol}: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# CORE ORDER LOGIC
# ──────────────────────────────────────────────────────────────────────────────
def place_buy_near_ma(symbol, qty, hma_hint=None):
    cancel_open_orders(symbol)
    for i in range(CHASE_REPRICES):
        bid, ask = get_nbbo(symbol)
        last = get_last(symbol)
        hma_live = hma_hint or last
        if not (bid or ask or last):
            time.sleep(CHASE_SLEEP_SEC)
            continue

        limit = hma_live * (1.0 + ENTRY_BUFFER_PCT)
        if ask:
            limit = max(limit, ask)
        limit = round_price(limit)

        try:
            api.submit_order(
                symbol=symbol, qty=qty, side="buy",
                type="limit", limit_price=str(limit),
                time_in_force="day", extended_hours=True
            )
            log(f"🟢 BUY {symbol} LMT @{limit}  (HMA≈{round_price(hma_live)})")
        except Exception as e:
            log(f"BUY submit error {symbol}: {e}")
            break

        time.sleep(CHASE_SLEEP_SEC)
        if get_position_qty(symbol) >= qty:
            log(f"✅ Filled BUY {symbol}")
            return True
        cancel_open_orders(symbol)

    log(f"⚠️ BUY chase ended {symbol} (no fill).")
    return False


def place_sell_near_ma(symbol, qty_hint=None, hma_hint=None, aggressive=False):
    qty = qty_hint or get_position_qty(symbol)
    if qty <= 0:
        log(f"ℹ️ No position to close for {symbol}")
        return True

    cancel_open_orders(symbol)
    for i in range(CHASE_REPRICES if aggressive else max(3, CHASE_REPRICES // 2)):
        bid, ask = get_nbbo(symbol)
        last = get_last(symbol)
        hma_live = hma_hint or last
        if not (bid or ask or last):
            time.sleep(CHASE_SLEEP_SEC / 2)
            continue

        limit = hma_live * (1.0 - EXIT_BUFFER_PCT)
        if aggressive and bid:
            limit = min(limit, bid * (1.0 - AGG_EXIT_EXTRA_PCT))
        limit = round_price(limit)

        try:
            api.submit_order(
                symbol=symbol, qty=qty, side="sell",
                type="limit", limit_price=str(limit),
                time_in_force="day", extended_hours=True
            )
            log(f"🛑 SELL {symbol} LMT @{limit}  (HMA≈{round_price(hma_live)})")
        except Exception as e:
            log(f"SELL submit error {symbol}: {e}")
            break

        time.sleep(CHASE_SLEEP_SEC)
        if get_position_qty(symbol) <= 0:
            log(f"✅ Position exited {symbol}")
            return True
        cancel_open_orders(symbol)

    log(f"⚠️ SELL chase ended {symbol} (may still be holding).")
    return False

# ──────────────────────────────────────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, mode="HMA+MaMA", add_shares=ADD_SHARES_QTY)

@app.post("/tv")
def tv():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403

    log(f"🚀 TradingView Alert:\n{json.dumps(data, indent=2)}")

    action  = (data.get("action") or data.get("event") or "").upper()
    symbol  = (data.get("ticker") or "").upper()
    qty     = float(data.get("quantity", 100))
    hma_hint = float(data.get("hma", 0.0)) if data.get("hma") else 0.0

    if not symbol or not action:
        return jsonify(ok=False, reason="missing symbol/action"), 400

    try:
        # === BUY or ADD ===
        if action == "BUY_SIGNAL":
            current_qty = get_position_qty(symbol)
            if current_qty > 0:
                # already holding: add to position
                log(f"➕ Already holding {symbol} ({current_qty} shares). Adding {ADD_SHARES_QTY} more.")
                place_buy_near_ma(symbol, ADD_SHARES_QTY, hma_hint=hma_hint)
                return jsonify(ok=True, added=ADD_SHARES_QTY)
            else:
                # open fresh 100-share position
                log(f"🟢 Opening new position {symbol} ({qty} shares).")
                place_buy_near_ma(symbol, qty, hma_hint=hma_hint)
                return jsonify(ok=True, opened=qty)

        # === EXIT ===
        elif action in ("EXIT_SIGNAL", "SELL", "TP"):
            log(f"🔻 Exit signal received for {symbol}.")
            exited = place_sell_near_ma(symbol, hma_hint=hma_hint, aggressive=False)
            return jsonify(ok=exited)

        # === PANIC ===
        elif action in ("PANIC", "AGGRESSIVE_EXIT"):
            log(f"🚨 Panic exit received for {symbol}! Forcing aggressive liquidation.")
            exited = place_sell_near_ma(symbol, hma_hint=hma_hint, aggressive=True)
            return jsonify(ok=exited)

        else:
            log(f"⚠️ Unknown action: {action}")
            return jsonify(ok=False, reason="unknown action")

    except Exception as e:
        log(f"❌ Handler error {symbol}: {e}")
        return jsonify(error=str(e)), 500

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)





















