# main.py
import os
import json
import time
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ──────────────────────────────────────────────────────────────────────────────
# Config / Env
# ──────────────────────────────────────────────────────────────────────────────
APP_SECRET = os.environ.get("WEBHOOK_SECRET", "CHRISBOT1501").upper()

API_KEY    = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
PAPER      = os.environ.get("ALPACA_PAPER", "true").lower() != "false"

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)  # base_url handled by SDK
data    = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# Constants
ET = timezone(timedelta(hours=-5))  # Eastern Time (no DST mgmt needed for bot logic simplicity)
EXIT_RETRY_SECONDS = 8              # per your choice
DAILY_LIQUIDATE_HHMM = (19, 59)     # 19:59 ET

# ──────────────────────────────────────────────────────────────────────────────
# State (in-memory; resets on deploy)
# ──────────────────────────────────────────────────────────────────────────────
# per-day loss counter
loss_counter = {}             # { "SYMBOL": {"date":"YYYY-MM-DD","losses": int} }
MAX_LOSSES_PER_TICKER = 2

# open average prices tracked (best-effort; we also query broker when needed)
position_cache = {}           # { "SYMBOL": avg_price_float }

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def log(msg, **extra):
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    if extra:
        print(f"[{stamp}] {msg} | {json.dumps(extra, ensure_ascii=False)}")
    else:
        print(f"[{stamp}] {msg}")

def today_et_str():
    return datetime.now(ET).strftime("%Y-%m-%d")

def reset_if_new_day(symbol):
    d = today_et_str()
    rec = loss_counter.get(symbol)
    if not rec or rec.get("date") != d:
        loss_counter[symbol] = {"date": d, "losses": 0}

def inc_loss(symbol):
    reset_if_new_day(symbol)
    loss_counter[symbol]["losses"] += 1

def losses_remaining(symbol):
    reset_if_new_day(symbol)
    return max(0, MAX_LOSSES_PER_TICKER - loss_counter[symbol]["losses"])

def get_latest_quote(symbol):
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        q = data.get_stock_latest_quote(req)
        # The SDK returns dict-like keyed by symbol for multi; for single it may return object
        # Normalize:
        if isinstance(q, dict):
            q = q.get(symbol)
        return {
            "bid": float(q.bid_price or 0.0),
            "ask": float(q.ask_price or 0.0),
            "mid": float(((q.bid_price or 0.0) + (q.ask_price or 0.0)) / 2.0),
        }
    except Exception as e:
        log("❗ get_latest_quote failed", symbol=symbol, error=str(e))
        return {"bid": 0.0, "ask": 0.0, "mid": 0.0}

def entry_buffer_dollars(ref_price):
    # ≥ $1 → $0.03 ; < $1 → $0.003
    return 0.03 if ref_price >= 1.0 else 0.003

def parse_float(value, default=None):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if "{{" in s and "}}" in s:   # template from TradingView not rendered
        return default
    try:
        return float(s)
    except:
        return default

def parse_quantity(value, default=100):
    q = parse_float(value, default)
    return int(q) if q and q > 0 else default

def good_symbol(sym):
    if not sym:
        return False
    s = str(sym).strip().upper()
    if "{{" in s and "}}" in s:
        return False
    return s.isalnum() or "-" in s or "." in s

def place_limit_buy(symbol, qty, source):
    q = get_latest_quote(symbol)
    ref = q["ask"] if q["ask"] > 0 else (q["mid"] if q["mid"] > 0 else q["bid"])
    if ref <= 0:
        raise ValueError(f"No quote to price BUY {symbol}")
    limit_price = round(ref + entry_buffer_dollars(ref), 4)
    order = trading.submit_order(
        order_data=LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            limit_price=limit_price,
            time_in_force=TimeInForce.DAY,
            extended_hours=True
        )
    )
    log("✅ BUY placed", action="BUY", symbol=symbol, qty=qty, limit=limit_price, source=source)
    return order

def force_exit_limit(symbol, qty_target, target_close_price=None, reason="SIGNAL_EXIT"):
    """
    Force-exit loop that never gives up:
      1) Try at target_close_price (if provided)
      2) Otherwise/then chase best bid every EXIT_RETRY_SECONDS until flat
    """
    # Resolve how many shares are actually held
    try:
        pos = trading.get_open_position(symbol)
        qty_open = int(float(pos.qty))
        avg_price = float(pos.avg_entry_price)
        position_cache[symbol] = avg_price
    except Exception:
        qty_open = 0

    if qty_open <= 0:
        log("ℹ️ No position to close", symbol=symbol)
        return

    def try_once_close_at(price, qty):
        price = round(float(price), 4)
        try:
            order = trading.submit_order(
                order_data=LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    limit_price=price,
                    time_in_force=TimeInForce.DAY,
                    extended_hours=True
                )
            )
            log("➡️ Exit attempt", symbol=symbol, qty=qty, limit=price, reason=reason)
            return True
        except Exception as e:
            log("❌ Exit submit failed", symbol=symbol, error=str(e), limit=price)
            return False

    # 1) Try at target close price (if valid)
    first_tried = False
    if target_close_price and target_close_price > 0:
        first_tried = try_once_close_at(target_close_price, qty_open)
        # Give a brief moment before we start chase loop
        time.sleep(1)

    # 2) Chase until flat
    safety_loops = 0
    while True:
        # refresh open qty
        try:
            pos = trading.get_open_position(symbol)
            qty_open = int(float(pos.qty))
            last_avg = float(pos.avg_entry_price)
            position_cache[symbol] = last_avg
        except Exception:
            qty_open = 0

        if qty_open <= 0:
            log("✅ Flat after exit", symbol=symbol, reason=reason)
            return

        # peg to bid for best chance to fill
        q = get_latest_quote(symbol)
        bid = q["bid"]
        if bid <= 0:
            # if we have no bid, try mid or last fallback
            bid = q["mid"] if q["mid"] > 0 else max(0.01, position_cache.get(symbol, 0.01) * 0.9)

        # small sweetener to increase fill probability (a tiny undercut is not possible on sell; we hit bid)
        target = bid

        try_once_close_at(target, qty_open)

        safety_loops += 1
        time.sleep(EXIT_RETRY_SECONDS)

        # safety print every few loops
        if safety_loops % 10 == 0:
            log("⏳ Still exiting...", symbol=symbol, loops=safety_loops, last_bid=bid)

def record_loss_if_applicable(symbol, exit_price):
    avg = position_cache.get(symbol)
    if avg is None:
        # try broker
        try:
            pos = trading.get_open_position(symbol)
            avg = float(pos.avg_entry_price)
        except Exception:
            avg = None
    if avg is None:
        return
    if exit_price < avg:
        inc_loss(symbol)
        log("📉 Loss recorded", symbol=symbol, losses=loss_counter[symbol]["losses"])

def daily_liquidation_daemon():
    while True:
        now = datetime.now(ET)
        hhmm = (now.hour, now.minute)
        if hhmm == DAILY_LIQUIDATE_HHMM:
            try:
                acct = trading.get_account()
                log("⚠️ Daily liquidation starting", equity=str(acct.equity))
            except Exception:
                log("⚠️ Daily liquidation starting")

            # get positions and exit each
            try:
                positions = trading.get_all_positions()
                for p in positions:
                    sym = p.symbol
                    qty = int(float(p.qty))
                    if qty > 0:
                        # target close unknown after hours; just chase immediately
                        threading.Thread(target=force_exit_limit, args=(sym, qty, None, "DAILY_19_59"), daemon=True).start()
            except Exception as e:
                log("❗ liquidation fetch positions failed", error=str(e))
            time.sleep(60)  # avoid multiple triggers in same minute
        time.sleep(5)

# start daily liquidation thread
threading.Thread(target=daily_liquidation_daemon, daemon=True).start()

# ──────────────────────────────────────────────────────────────────────────────
# Flask
# ──────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.get("/health")
def health():
    try:
        acct = trading.get_account()
        return jsonify({"ok": True, "status": "ACTIVE", "equity": str(acct.equity)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/tv")
def tv():
    raw = request.get_data(as_text=True) or ""
    log("📩 Webhook inbound", raw=raw)

    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        log("❌ JSON parse failed")
        return jsonify({"ok": False, "error": "invalid json"}), 400

    secret = str(payload.get("secret", "")).upper().strip()
    if secret != APP_SECRET:
        log("🚫 Unauthorized webhook", got=secret)
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    action = str(payload.get("action", "")).upper().strip()
    symbol = payload.get("ticker") or payload.get("symbol") or ""
    symbol = str(symbol).upper().strip()

    if not good_symbol(symbol):
        log("❌ Bad symbol in payload", symbol=symbol)
        return jsonify({"ok": False, "error": "bad symbol"}), 400

    qty = parse_quantity(payload.get("quantity", 100), 100)
    source = str(payload.get("source", "UNKNOWN")).upper().strip()

    # optional: may be template; parse_float handles templates -> None
    signal_close = parse_float(payload.get("signal_close"), None)

    # Loss lockout
    if action == "BUY" and losses_remaining(symbol) <= 0:
        log("🛑 Loss cap reached; ignoring BUY", symbol=symbol)
        return jsonify({"ok": False, "ignored": "loss_cap_reached"}), 200

    try:
        if action == "BUY":
            # VBTS or SMOOTH_HA buys: limit-only w/ buffer from current ask
            order = place_limit_buy(symbol, qty, source)
            return jsonify({"ok": True, "order_id": order.id, "symbol": symbol})
        elif action == "SELL":
            # Force-exit loop (try signal_close first, then chase)
            threading.Thread(
                target=force_exit_limit,
                args=(symbol, qty, signal_close, source or "SIGNAL_EXIT"),
                daemon=True
            ).start()
            return jsonify({"ok": True, "exit_started": True, "symbol": symbol})
        else:
            log("❌ Unknown action", action=action)
            return jsonify({"ok": False, "error": "unknown action"}), 400
    except Exception as e:
        log("❌ Handler failed", error=str(e))
        return jsonify({"ok": False, "error": str(e)}), 500

# ──────────────────────────────────────────────────────────────────────────────
# Gunicorn entrypoint
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Local run
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))










































































