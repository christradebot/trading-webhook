from flask import Flask, request, jsonify
import os, json, time, threading, math, traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca_trade_api.rest import REST

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Environment / Clients
# ──────────────────────────────────────────────────────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")

NY = ZoneInfo("America/New_York")

# State for background monitors
position_threads_lock = threading.Lock()
position_threads = {}   # symbol -> thread
exit_threads = {}       # symbol -> thread

# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def ts():
    return datetime.now(NY).strftime("[%H:%M:%S]")

def round_tick(p: float) -> float:
    """Alpaca rejects sub-penny prices. Use $0.01 ticks."""
    if p is None or math.isnan(p):
        return None
    return round(float(p) + 1e-9, 2)

def is_rth(dt: datetime | None = None) -> bool:
    dt = dt or datetime.now(NY)
    start = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    end   = dt.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= dt <= end

def latest_bid_ask(symbol: str):
    try:
        q = api.get_latest_quote(symbol)
        bid = q.bidprice or None
        ask = q.askprice or None
        return (bid, ask)
    except Exception:
        return (None, None)

def safe_qty(symbol: str) -> float:
    try:
        pos = api.get_position(symbol)
        return float(pos.qty)
    except Exception:
        return 0.0

def log(msg: str):
    print(f"{ts()} {msg}", flush=True)

def submit_limit(side: str, symbol: str, qty: float, limit_price: float, extended: bool):
    limit_price = round_tick(limit_price)
    tif = "day"  # Extended hours requires DAY
    try:
        o = api.submit_order(
            symbol=symbol,
            side=side,
            qty=qty,
            type="limit",
            time_in_force=tif,
            limit_price=limit_price,
            extended_hours=extended
        )
        return o
    except Exception as e:
        log(f"❌ {side.upper()} submit error {symbol}: {e}")
        return None

def submit_market(side: str, symbol: str, qty: float):
    try:
        o = api.submit_order(
            symbol=symbol,
            side=side,
            qty=qty,
            type="market",
            time_in_force="day",
            extended_hours=False  # market orders only RTH
        )
        return o
    except Exception as e:
        log(f"❌ {side.upper()} submit error {symbol}: {e}")
        return None

def cancel_all_for(symbol: str):
    try:
        open_orders = api.list_orders(status="open", symbols=[symbol])
    except TypeError:
        # Older SDKs may not support symbols filter
        open_orders = [o for o in api.list_orders(status="open") if o.symbol == symbol]
    for o in open_orders:
        try:
            api.cancel_order(o.id)
            log(f"🧹 Cancelled open order: {symbol}")
        except Exception:
            pass

# ──────────────────────────────────────────────────────────────────────────────
# Managed EXIT (10-minute window)
# ──────────────────────────────────────────────────────────────────────────────
def managed_exit(symbol: str, qty_hint: float, vwap: float, mama: float):
    """Try to exit near the closer of VWAP/MaMA for up to 10 minutes.
       Pre/after-hours: limit chase only. RTH: limit then market failsafe."""
    try:
        # Determine target (closest to current price)
        bid, ask = latest_bid_ask(symbol)
        last = ask if ask else bid
        if last is None:
            last = float(api.get_last_trade(symbol).price)

        target = None
        candidates = [p for p in [vwap, mama] if p and not math.isnan(p)]
        if candidates:
            target = min(candidates, key=lambda p: abs(p - last))

        # Fallback: if we somehow didn't receive levels, use bid
        if target is None:
            target = bid if bid else last

        target = round_tick(target)
        qty = safe_qty(symbol)
        if qty <= 0 and qty_hint:
            qty = qty_hint

        if qty <= 0:
            log(f"ℹ️ No position to close for {symbol}")
            return

        start = datetime.now(NY)
        end = start + timedelta(minutes=10)

        log(f"🟣 Target exit {symbol} {target} (chosen from VWAP/MaMA)")

        while datetime.now(NY) < end:
            # Refresh remaining qty
            rem = safe_qty(symbol)
            if rem <= 0:
                log(f"✅ Position exited {symbol}")
                return

            cancel_all_for(symbol)

            now_rth = is_rth()
            bid, ask = latest_bid_ask(symbol)

            # Price to post: a touch is fine — we sit at min(target, bid) for sells
            if bid:
                post = min(target, bid)  # be conservative to get fills on sells
            else:
                post = target

            o = submit_limit("sell", symbol, rem, post, extended=not now_rth)
            if o is None:
                # If limit was rejected (tick, etc.), nudge by 1 cent
                post = round_tick(post - 0.01)
                submit_limit("sell", symbol, rem, post, extended=not now_rth)

            time.sleep(20)  # wait, then re-check

        # 10 minutes passed — force out
        rem = safe_qty(symbol)
        if rem > 0:
            if is_rth():
                log(f"⚠️ Still open after 10 min, forcing exit MARKET for {symbol}")
                submit_market("sell", symbol, rem)
            else:
                # Extended hours — force an aggressive limit just under bid
                bid, _ = latest_bid_ask(symbol)
                aggressive = round_tick((bid if bid else target) - 0.01)
                log(f"⚠️ Still open after 10 min, forcing exit LMT {aggressive} (XH) for {symbol}")
                submit_limit("sell", symbol, rem, aggressive, extended=True)
    except Exception as e:
        log(f"❌ managed_exit error {symbol}: {e}\n{traceback.format_exc()}")

# ──────────────────────────────────────────────────────────────────────────────
# 10% Hard Stop monitor
# ──────────────────────────────────────────────────────────────────────────────
def stop_monitor(symbol: str, entry_price: float):
    """Background watcher; if price <= 90% of entry, exit immediately (rules by session)."""
    try:
        threshold = entry_price * 0.90
        while True:
            qty = safe_qty(symbol)
            if qty <= 0:
                return  # position closed

            bid, ask = latest_bid_ask(symbol)
            last = bid if bid else ask
            if last is None:
                try:
                    last = float(api.get_last_trade(symbol).price)
                except Exception:
                    last = None

            if last is not None and last <= threshold:
                if is_rth():
                    log(f"🛑 Hard stop (−10%) hit {symbol} @~{last} — MARKET")
                    submit_market("sell", symbol, qty)
                else:
                    # Extended hours: aggressive limit chase
                    price = round_tick((bid if bid else last) - 0.01)
                    log(f"🛑 Hard stop (−10%) hit {symbol} — LIMIT chase from {price} (XH)")
                    # quick chase loop up to 2 minutes
                    end = datetime.now(NY) + timedelta(minutes=2)
                    while datetime.now(NY) < end and safe_qty(symbol) > 0:
                        cancel_all_for(symbol)
                        submit_limit("sell", symbol, safe_qty(symbol), price, extended=True)
                        time.sleep(10)
                        bid, _ = latest_bid_ask(symbol)
                        if bid and bid < price:
                            price = round_tick(bid - 0.01)
                return

            time.sleep(5)
    except Exception as e:
        log(f"❌ stop_monitor error {symbol}: {e}")

def ensure_thread(d: dict, key: str, target, *args):
    with position_threads_lock:
        t = d.get(key)
        if t and t.is_alive():
            return
        t = threading.Thread(target=target, args=args, daemon=True)
        d[key] = t
        t.start()

# ──────────────────────────────────────────────────────────────────────────────
# Webhook
# ──────────────────────────────────────────────────────────────────────────────
@app.route("/tv", methods=["POST"])
def tv():
    try:
        data = request.get_json(force=True, silent=True) or {}
        log(f"🚀 TradingView Alert:\n{json.dumps(data, indent=2)}")

        # Validate
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "invalid secret"}), 403

        action   = str(data.get("action", "")).upper()
        symbol   = str(data.get("ticker", "")).upper()
        qty      = float(data.get("quantity", 0) or 0)
        vwap     = float(data.get("vwap", "nan"))
        mama     = float(data.get("mama", "nan"))
        upper    = float(data.get("upper", "nan"))

        if not symbol or action not in ("BUY", "EXIT"):
            return jsonify({"error": "bad payload"}), 400

        # Decide session
        rth = is_rth()

        if action == "BUY":
            # Choose the lowest among upper / vwap / mama (enter at or below ‘equilibrium’)
            candidates = [p for p in [upper, vwap, mama] if p and not math.isnan(p)]
            if not candidates:
                return jsonify({"error": "no levels in payload"}), 400

            entry_limit = min(candidates)
            entry_limit = round_tick(entry_limit)

            log(f"📈 BUY {symbol} LMT @{entry_limit}  (VWAP={round_tick(vwap)} MAMA={round_tick(mama)} UPPER={round_tick(upper)})")
            cancel_all_for(symbol)
            o = submit_limit("buy", symbol, qty, entry_limit, extended=not rth)
            if o is None:
                return jsonify({"status": "buy_rejected"}), 200

            # Wait briefly and confirm position; then spawn stop monitor
            time.sleep(2)
            try:
                pos = api.get_position(symbol)
                entry = float(pos.avg_entry_price)
                ensure_thread(position_threads, symbol, stop_monitor, symbol, entry)
                log(f"✅ Filled BUY {symbol}")
            except Exception:
                log(f"🕒 Monitoring {symbol} for fill (stop watcher will start after fill)")

            return jsonify({"status": "buy_ok"}), 200

        # EXIT management (10 min window)
        if action == "EXIT":
            log(f"🔔 EXIT triggered for {symbol}")
            ensure_thread(exit_threads, symbol, managed_exit, symbol, qty, vwap, mama)
            return jsonify({"status": "exit_started"}), 200

        return jsonify({"status": "ignored"}), 200

    except Exception as e:
        log(f"❌ Handler error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": "server_error"}), 500

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Local run (Railway uses gunicorn)
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)























