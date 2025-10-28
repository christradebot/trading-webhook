# =========================
# main.py — Athena + Chris 2025
# ITG Scalper Bot (Clean + Secure Secret + Alpaca Feed)
# =========================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
from datetime import datetime, timedelta
import os, time, json, threading, traceback, pytz, hmac

# ──────────────────────────────
# Environment setup
# ──────────────────────────────
ALPACA_KEY_ID     = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = (os.getenv("WEBHOOK_SECRET") or "").strip()

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
app = Flask(__name__)
NY  = pytz.timezone("America/New_York")

# ──────────────────────────────
# State
# ──────────────────────────────
stops, watchers, lock = {}, {}, threading.Lock()

# ──────────────────────────────
# Helpers
# ──────────────────────────────
def log(msg): print(f"{datetime.now(NY).strftime('%H:%M:%S')} | {msg}", flush=True)

def round_tick(px):
    return round(px, 4) if px < 1 else round(px, 2)

def latest_bid_ask(sym):
    try:
        q = api.get_latest_quote(sym)
        bid = float(q.bidprice or 0)
        ask = float(q.askprice or 0)
        return bid, ask
    except Exception:
        return 0.0, 0.0

def last_trade(sym):
    try:
        t = api.get_latest_trade(sym)
        return float(getattr(t, "price", 0) or 0)
    except Exception:
        return 0.0

def safe_qty(sym):
    try:
        pos = api.get_position(sym)
        return float(pos.qty)
    except Exception:
        return 0.0

def avg_entry(sym):
    try:
        pos = api.get_position(sym)
        return float(pos.avg_entry_price)
    except Exception:
        return 0.0

def pnl_record(sym, px_exit):
    try:
        q = safe_qty(sym)
        entry = avg_entry(sym)
        pnl = (px_exit - entry) * q
        log(f"💰 {sym} closed @{round_tick(px_exit)} | avg {round_tick(entry)} | Δ ${pnl:.2f}")
    except Exception:
        pass

# ──────────────────────────────
# Stop / time logic
# ──────────────────────────────
def within_vol_window():
    now = datetime.now(NY).time()
    return datetime.strptime("09:30","%H:%M").time() <= now <= datetime.strptime("09:45","%H:%M").time()

def get_stop(entry, low):
    if within_vol_window():
        guard = entry * 0.03
        return round_tick(min(low, entry - guard))
    return round_tick(low)

# ──────────────────────────────
# Orders
# ──────────────────────────────
def submit_limit(side, sym, qty, price):
    try:
        api.submit_order(
            symbol=sym,
            qty=int(qty),
            side=side,
            type="limit",
            time_in_force="day",
            limit_price=round_tick(price),
            extended_hours=True
        )
        log(f"📥 {side.upper()} {sym} @{round_tick(price)} x{int(qty)}")
    except Exception as e:
        log(f"⚠️ submit_limit({sym}): {e}")

def cancel_all(sym):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == sym:
                api.cancel_order(o.id)
    except Exception:
        pass

# ──────────────────────────────
# Managed exit
# ──────────────────────────────
def managed_exit(sym, qty_hint, target_price=None):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0:
            return
        price = target_price or last_trade(sym) or 0
        if price <= 0:
            bid, ask = latest_bid_ask(sym)
            price = bid or ask
        if price <= 0:
            return
        cancel_all(sym)
        submit_limit("sell", sym, qty, price)
        time.sleep(6)
        if safe_qty(sym) <= 0:
            pnl_record(sym, price)
            log(f"✅ EXIT filled {sym}")
            return
        # fallback
        px = price
        step = 0.0005 if px < 1 else 0.02
        for _ in range(25):
            if safe_qty(sym) <= 0: break
            px = round_tick(px - step)
            cancel_all(sym)
            submit_limit("sell", sym, qty, px)
            time.sleep(2)
        pnl_record(sym, px)
    except Exception as e:
        log(f"❌ managed_exit({sym}): {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# Stop watcher
# ──────────────────────────────
def stop_watcher(sym):
    log(f"👀 watching stop for {sym}")
    try:
        while True:
            time.sleep(5)
            s = stops.get(sym)
            if not s: break
            if safe_qty(sym) <= 0:
                stops.pop(sym, None)
                break
            last = last_trade(sym)
            if last <= 0: continue
            if last <= s["stop"]:
                log(f"🛑 Stop hit {sym}: {last} ≤ {s['stop']}")
                managed_exit(sym, safe_qty(sym), s["stop"])
                stops.pop(sym, None)
                break
    except Exception as e:
        log(f"stop_watcher err: {e}")

def ensure_watcher(sym):
    if sym in watchers and watchers[sym].is_alive():
        return
    t = threading.Thread(target=stop_watcher, args=(sym,), daemon=True)
    watchers[sym] = t
    t.start()

# ──────────────────────────────
# Trade logic
# ──────────────────────────────
def execute_buy(sym, qty, entry_price, candle_low):
    if safe_qty(sym) > 0:
        log(f"⏩ already in {sym}")
        return
    stop = get_stop(entry_price, candle_low)
    log(f"🟢 BUY {sym} @{round_tick(entry_price)} | stop {stop}")
    submit_limit("buy", sym, qty, entry_price)
    stops[sym] = {"stop": stop}
    ensure_watcher(sym)

def execute_add(sym, qty, entry_price):
    if safe_qty(sym) <= 0:
        log(f"⚠️ ADD ignored {sym}, no position")
        return
    log(f"➕ ADD {sym} @{round_tick(entry_price)}")
    submit_limit("buy", sym, qty, entry_price)

def execute_exit(sym, qty, exit_price):
    log(f"🔔 EXIT {sym} @{round_tick(exit_price)}")
    managed_exit(sym, qty, exit_price)

# ──────────────────────────────
# Alert handler
# ──────────────────────────────
def handle_alert(data):
    try:
        sym   = (data.get("ticker") or "").upper()
        act   = (data.get("action") or "").upper()
        qty   = float(data.get("quantity", 100))
        entry = float(data.get("entry_price", 0))
        low   = float(data.get("candle_low", 0))
        exitp = float(data.get("exit_price", 0))

        if act == "BUY":
            execute_buy(sym, qty, entry, low)
        elif act == "ADD":
            execute_add(sym, qty, entry)
        elif act == "EXIT":
            execute_exit(sym, qty, exitp)
        else:
            log(f"⚠️ unknown action {act}")
    except Exception as e:
        log(f"❌ handle_alert: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# Secure /tv endpoint
# ──────────────────────────────
def _extract_secret(req_json, req_headers):
    body_secret = str((req_json or {}).get("secret", "")).strip()
    header_secret = str(req_headers.get("X-Webhook-Secret", "")).strip()
    return body_secret or header_secret

def _mask(s): return f"len={len(s)} start='{s[:1]}' end='{s[-1:]}'"

@app.post("/tv")
def tv():
    try:
        data = request.get_json(force=True, silent=True) or {}
        incoming = _extract_secret(data, request.headers)
        ok = WEBHOOK_SECRET and incoming and hmac.compare_digest(incoming, WEBHOOK_SECRET)
        print(f"[tv] secret check -> incoming({_mask(incoming)}) vs expected({_mask(WEBHOOK_SECRET)})", flush=True)
        if not ok:
            return jsonify(error="Invalid secret"), 403
        threading.Thread(target=handle_alert, args=(data,), daemon=True).start()
        return jsonify(ok=True), 200
    except Exception as e:
        log(f"❌ /tv error: {e}\n{traceback.format_exc()}")
        return jsonify(err="server"), 500

# ──────────────────────────────
# Ping
# ──────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, base=ALPACA_BASE_URL)

# ──────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))







































