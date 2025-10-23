from flask import Flask, request, jsonify
import os, json, time, threading, math, traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from alpaca_trade_api.rest import REST

app = Flask(__name__)

# ───────────────────────────────
# Environment Variables
# ───────────────────────────────
ALPACA_KEY_ID = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
NY = ZoneInfo("America/New_York")

# Thread locks
position_threads_lock = threading.Lock()
position_threads = {}
exit_threads = {}

# ───────────────────────────────
# Helpers
# ───────────────────────────────
def ts():
    return datetime.now(NY).strftime("[%H:%M:%S]")

def log(msg):
    print(f"{ts()} {msg}", flush=True)

def round_tick(price):
    if price is None or math.isnan(price):
        return None
    return round(float(price) + 1e-9, 2)

def is_rth(dt=None):
    dt = dt or datetime.now(NY)
    start = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    end = dt.replace(hour=16, minute=0, second=0, microsecond=0)
    return start <= dt <= end

def latest_bid_ask(symbol):
    try:
        q = api.get_latest_quote(symbol)
        return (q.bidprice or None, q.askprice or None)
    except Exception:
        return (None, None)

def latest_trade_price(symbol):
    try:
        t = api.get_latest_trade(symbol)
        return float(t.price)
    except Exception:
        return None

def safe_qty(symbol):
    try:
        pos = api.get_position(symbol)
        return float(pos.qty)
    except Exception:
        return 0.0

def cancel_all_for(symbol):
    try:
        open_orders = api.list_orders(status="open")
        for o in open_orders:
            if o.symbol == symbol:
                api.cancel_order(o.id)
                log(f"🧹 Cancelled open order: {symbol}")
    except Exception:
        pass

def submit_limit(side, symbol, qty, limit_price, extended):
    limit_price = round_tick(limit_price)
    try:
        return api.submit_order(
            symbol=symbol,
            side=side,
            qty=qty,
            type="limit",
            time_in_force="day",
            limit_price=limit_price,
            extended_hours=extended
        )
    except Exception as e:
        log(f"❌ {side.upper()} submit error {symbol}: {e}")
        return None

def submit_market(side, symbol, qty):
    try:
        return api.submit_order(
            symbol=symbol,
            side=side,
            qty=qty,
            type="market",
            time_in_force="day",
            extended_hours=False
        )
    except Exception as e:
        log(f"❌ {side.upper()} submit error {symbol}: {e}")
        return None

# ───────────────────────────────
# Managed Exit Logic (10-Minute Timer)
# ───────────────────────────────
def managed_exit(symbol, qty_hint, vwap, mama):
    try:
        bid, ask = latest_bid_ask(symbol)
        last = ask or bid or latest_trade_price(symbol)
        if last is None:
            last = 0

        candidates = [p for p in [vwap, mama] if p and not math.isnan(p)]
        target = min(candidates, key=lambda p: abs(p - last)) if candidates else last
        target = round_tick(target)

        qty = safe_qty(symbol) or qty_hint
        if qty <= 0:
            log(f"ℹ️ No position to close for {symbol}")
            return

        log(f"🟣 Target exit {symbol} {target} (chosen from VWAP/MaMA)")
        start = datetime.now(NY)
        end = start + timedelta(minutes=10)

        while datetime.now(NY) < end:
            rem = safe_qty(symbol)
            if rem <= 0:
                log(f"✅ Position exited {symbol}")
                return

            cancel_all_for(symbol)
            rth = is_rth()
            bid, ask = latest_bid_ask(symbol)
            post = min(target, bid) if bid else target
            submit_limit("sell", symbol, rem, post, extended=not rth)
            time.sleep(20)

        rem = safe_qty(symbol)
        if rem > 0:
            if is_rth():
                log(f"⚠️ Still open after 10 min, forcing MARKET exit {symbol}")
                submit_market("sell", symbol, rem)
            else:
                bid, _ = latest_bid_ask(symbol)
                aggressive = round_tick((bid if bid else target) - 0.01)
                log(f"⚠️ Still open after 10 min, forcing LIMIT exit {symbol} @ {aggressive}")
                submit_limit("sell", symbol, rem, aggressive, extended=True)

    except Exception as e:
        log(f"❌ managed_exit error {symbol}: {e}\n{traceback.format_exc()}")

# ───────────────────────────────
# Hard Stop Monitor (−10%)
# ───────────────────────────────
def stop_monitor(symbol, entry_price):
    try:
        threshold = entry_price * 0.90
        while True:
            qty = safe_qty(symbol)
            if qty <= 0:
                return

            bid, ask = latest_bid_ask(symbol)
            last = bid or ask or latest_trade_price(symbol)
            if last is None:
                time.sleep(5)
                continue

            if last <= threshold:
                if is_rth():
                    log(f"🛑 Hard stop hit {symbol} @~{last} — MARKET")
                    submit_market("sell", symbol, qty)
                else:
                    price = round_tick((bid if bid else last) - 0.01)
                    log(f"🛑 Hard stop hit {symbol} — LIMIT chase from {price} (XH)")
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

def ensure_thread(thread_dict, key, target, *args):
    with position_threads_lock:
        t = thread_dict.get(key)
        if t and t.is_alive():
            return
        t = threading.Thread(target=target, args=args, daemon=True)
        thread_dict[key] = t
        t.start()

# ───────────────────────────────
# Webhook Endpoint
# ───────────────────────────────
@app.route("/tv", methods=["POST"])
def tv():
    try:
        data = request.get_json(force=True, silent=True) or {}
        log(f"🚀 TradingView Alert:\n{json.dumps(data, indent=2)}")

        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "invalid secret"}), 403

        action = str(data.get("action", "")).upper()
        symbol = str(data.get("ticker", "")).upper()
        qty = float(data.get("quantity", 0) or 0)
        vwap = float(data.get("vwap", "nan"))
        mama = float(data.get("mama", "nan"))
        upper = float(data.get("upper", "nan"))

        if not symbol or action not in ("BUY", "EXIT"):
            return jsonify({"error": "bad payload"}), 400

        rth = is_rth()

        # ──────────────── BUY ────────────────
        if action == "BUY":
            candidates = [p for p in [upper, vwap, mama] if p and not math.isnan(p)]
            if not candidates:
                return jsonify({"error": "no valid levels"}), 400
            entry_limit = round_tick(min(candidates))
            log(f"📈 BUY {symbol} LMT @{entry_limit} (VWAP={round_tick(vwap)} MAMA={round_tick(mama)} UPPER={round_tick(upper)})")

            cancel_all_for(symbol)
            submit_limit("buy", symbol, qty, entry_limit, extended=not rth)
            time.sleep(2)

            try:
                pos = api.get_position(symbol)
                entry = float(pos.avg_entry_price)
                ensure_thread(position_threads, symbol, stop_monitor, symbol, entry)
                log(f"✅ Filled BUY {symbol}")
            except Exception:
                log(f"🕒 Monitoring {symbol} for fill confirmation")

            return jsonify({"status": "buy_ok"}), 200

        # ──────────────── EXIT ────────────────
        if action == "EXIT":
            log(f"🔔 EXIT triggered for {symbol}")
            ensure_thread(exit_threads, symbol, managed_exit, symbol, qty, vwap, mama)
            return jsonify({"status": "exit_started"}), 200

        return jsonify({"status": "ignored"}), 200

    except Exception as e:
        log(f"❌ Handler error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": "server_error"}), 500

# ───────────────────────────────
# Health endpoint (optional)
# ───────────────────────────────
@app.route("/ping")
def ping():
    return jsonify(ok=True, time=ts(), service="tv→alpaca")

# ───────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)























