# =========================
# main.py — v3.0 (Athena + Chris 2025)
# ITG Scalper Bot (limit-only) + Hammer + ATR window + PnL + Option-2 exit
# =========================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
from datetime import datetime, timedelta
import os, time, json, traceback, pytz, threading, math

# ──────────────────────────────
# Environment + API setup
# ──────────────────────────────
ALPACA_KEY_ID = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
app = Flask(__name__)
NY = pytz.timezone("America/New_York")

# ──────────────────────────────
# State
# ──────────────────────────────
open_add_tracker = {} # one add per ticker (resets when flat)
loss_tracker = {} # max two losses per ticker per session
stops = {} # {sym: {"stop": float, "entry": float}}
watchers = {} # {sym: threading.Thread}
lock = threading.Lock()

# ──────────────────────────────
# Helpers
# ──────────────────────────────
def log(msg: str):
    print(f"{datetime.now(NY).strftime('%H:%M:%S')} | {msg}", flush=True)

def round_tick(px: float) -> float:
    if px is None:
        return 0.0
    # penny/sub-penny precision handling
    if px >= 1.0:
        return round(px, 2)
    elif px >= 0.1:
        return round(px, 3)
    else:
        return round(px, 4)

def price_tick(px: float) -> float:
    if px >= 1.0:
        return 0.01
    elif px >= 0.1:
        return 0.001
    else:
        return 0.0001

def latest_bid_ask(sym):
    try:
        q = api.get_latest_quote(sym)
        bid = float(q.bidprice or 0.0)
        ask = float(q.askprice or 0.0)
        return bid, ask
    except Exception:
        return 0.0, 0.0

def last_trade_price(sym):
    # Prefer bid for sells; else last trade
    bid, ask = latest_bid_ask(sym)
    if bid > 0:
        return bid
    try:
        t = api.get_latest_trade(sym)
        return float(getattr(t, "price", 0.0) or 0.0)
    except Exception:
        return 0.0

def safe_qty(sym):
    try:
        pos = api.get_position(sym)
        return float(pos.qty)
    except Exception:
        return 0.0

def avg_entry_price(sym):
    try:
        pos = api.get_position(sym)
        return float(pos.avg_entry_price)
    except Exception:
        return 0.0

def in_profit(sym):
    try:
        cur = last_trade_price(sym)
        avg = avg_entry_price(sym)
        return (cur > 0) and (avg > 0) and (cur > avg)
    except Exception:
        return False

def cancel_all(sym):
    try:
        for o in api.list_orders(status="open", symbols=[sym]):
            api.cancel_order(o.id)
    except Exception:
        pass

def record_loss(sym):
    with lock:
        loss_tracker[sym] = loss_tracker.get(sym, 0) + 1
        if loss_tracker[sym] >= 2:
            log(f"🚫 {sym} locked out after 2 losses.")

def can_trade(sym) -> bool:
    return loss_tracker.get(sym, 0) < 2

def within_atr_window() -> bool:
    now = datetime.now(NY).time()
    a = datetime.strptime("09:30", "%H:%M").time()
    b = datetime.strptime("09:45", "%H:%M").time()
    return a <= now <= b

def valid_candle_range(candle_close: float, candle_low: float) -> bool:
    if candle_close <= 0:
        return False
    rng = (candle_close - candle_low) / candle_close * 100.0
    log(f"🔎 Entry range (low→close): {rng:.2f}%")
    return rng <= 10.0

def get_stop(candle_low: float, candle_close: float, atr_value: float or None) -> float:
    """
    Use ATR×3 buffer only between 09:30–09:45 ET (wider, not tighter).
    Outside that window, strict candle low.
    """
    cl_low = float(candle_low)
    cl_close = float(candle_close)
    if within_atr_window() and atr_value is not None:
        widened = max(cl_low, cl_close - 3.0 * float(atr_value))
        stop_px = min(cl_close, widened)
    else:
        stop_px = cl_low
    return round_tick(stop_px)

# ──────────────────────────────
# Orders (limit-only)
# ──────────────────────────────
def submit_limit(side: str, sym: str, qty: float, px: float):
    try:
        api.submit_order(
            symbol=sym,
            qty=str(int(qty)),
            side=side,
            type="limit",
            time_in_force="day",
            limit_price=str(round_tick(px)),
            extended_hours=True
        )
        log(f"📥 {side.upper()} LIMIT {sym} @ {round_tick(px)} x{int(qty)}")
    except Exception as e:
        log(f"⚠️ submit_limit {sym}: {e}")

# ──────────────────────────────
# PnL logging
# ──────────────────────────────
def log_pnl(sym: str, exit_price: float, reason: str):
    try:
        # Try to fetch position just before it goes flat (best-effort)
        qty_before = safe_qty(sym)
        avg = avg_entry_price(sym)
        pnl_d = (float(exit_price) - float(avg)) * float(qty_before)
        pnl_p = ((float(exit_price) / float(avg)) - 1.0) * 100.0 if avg else 0.0
        log(f"💰 {sym} EXIT filled @{round_tick(exit_price)} (avg {round_tick(avg)}) "
            f"| PnL {round(pnl_d,2)} ({round(pnl_p,2)}%) | {reason}")
    except Exception:
        pass

# ──────────────────────────────
# Managed Exit (Option-2: target first, then aggressive loop)
# ──────────────────────────────
def managed_exit(sym: str, qty_hint: float, target_price: float = None, mark_stop_loss: bool = False, reason: str = "EXIT"):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0:
            return

        # 1) Try the target (from alert or stop)
        limit_px = None
        if target_price is not None and target_price > 0:
            limit_px = round_tick(target_price)
            log(f"🟣 Exit target for {sym} @ {limit_px}")
            cancel_all(sym)
            submit_limit("sell", sym, qty, limit_px)
            time.sleep(8)
            if safe_qty(sym) <= 0:
                log_pnl(sym, limit_px, f"{reason}_TARGET")
                with lock:
                    stops.pop(sym, None); open_add_tracker.pop(sym, None)
                if mark_stop_loss:
                    record_loss(sym)
                return

        # 2) Aggressive limit loop
        bid, ask = latest_bid_ask(sym)
        ref = bid or ask or limit_px or last_trade_price(sym)
        if ref <= 0:
            ref = limit_px or 0.01
        step = price_tick(ref) # one tick per step
        end_time = datetime.now(NY) + timedelta(minutes=5)
        px = ref
        tries = 0
        while datetime.now(NY) < end_time and safe_qty(sym) > 0:
            # for sells, keep stepping down by one tick from current bid/last
            cur_bid, cur_ask = latest_bid_ask(sym)
            base = cur_bid or last_trade_price(sym) or px
            px = max(round_tick(base - step), step)
            cancel_all(sym)
            submit_limit("sell", sym, safe_qty(sym), px)
            tries += 1
            log(f"⏱ Aggressive EXIT {sym} try {tries} @ {px}")
            time.sleep(3)

        # Final status
        if safe_qty(sym) <= 0:
            log_pnl(sym, px, f"{reason}_AGGR")
            with lock:
                stops.pop(sym, None); open_add_tracker.pop(sym, None)
            if mark_stop_loss:
                record_loss(sym)
        else:
            log(f"⚠️ Could not close {sym} fully.")
    except Exception as e:
        log(f"❌ managed_exit {sym}: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# Background Stop Watcher
# ──────────────────────────────
def stop_watcher(sym: str):
    log(f"👀 Stop watcher started for {sym}")
    try:
        while True:
            time.sleep(2)
            with lock:
                info = stops.get(sym)
            if info is None:
                break
            if safe_qty(sym) <= 0:
                with lock:
                    stops.pop(sym, None)
                break

            stop_px = info["stop"]
            last = last_trade_price(sym)
            if last <= 0:
                continue

            if last <= stop_px:
                log(f"🛑 Stop hit for {sym}: last {round_tick(last)} <= stop {round_tick(stop_px)}")
                managed_exit(sym, safe_qty(sym), target_price=stop_px, mark_stop_loss=True, reason="STOP")
                break
    except Exception as e:
        log(f"❌ stop_watcher {sym}: {e}\n{traceback.format_exc()}")
    finally:
        log(f"🧹 Stop watcher ended for {sym}")

def ensure_watcher(sym: str):
    with lock:
        t = watchers.get(sym)
        if t and t.is_alive():
            return
        t = threading.Thread(target=stop_watcher, args=(sym,), daemon=True)
        watchers[sym] = t
        t.start()

# ──────────────────────────────
# Trade actions
# ──────────────────────────────
def execute_buy(sym: str, qty: float, entry_price: float, candle_low: float, candle_close: float, atr_val: float or None):
    if not can_trade(sym):
        log(f"🚫 Skipping {sym}: reached loss limit.")
        return
    if safe_qty(sym) > 0:
        log(f"⏩ Already in position {sym}, skip BUY.")
        return
    if not valid_candle_range(candle_close, candle_low):
        log(f"🚫 {sym} BUY blocked: low→close > 10%.")
        return

    stop_price = get_stop(candle_low, candle_close, atr_val)
    log(f"🟢 BUY {sym} @ {round_tick(entry_price)} | Stop {round_tick(stop_price)}")
    submit_limit("buy", sym, qty, entry_price)

    with lock:
        stops[sym] = {"stop": stop_price, "entry": round_tick(entry_price)}
    ensure_watcher(sym)

def execute_add(sym: str, qty: float, entry_price: float):
    if safe_qty(sym) <= 0:
        log(f"ℹ️ No open position for {sym}, skip ADD.")
        return
    if open_add_tracker.get(sym):
        log(f"ℹ️ Add already used for {sym}.")
        return
    if not in_profit(sym):
        log(f"ℹ️ {sym} not in profit, skip ADD.")
        return

    log(f"➕ ADD {sym} @ {round_tick(entry_price)}")
    submit_limit("buy", sym, qty, entry_price)
    open_add_tracker[sym] = True

def handle_exit(sym: str, qty_hint: float, exit_price: float or None):
    managed_exit(sym, qty_hint, target_price=exit_price, mark_stop_loss=False, reason="EXIT")

# ──────────────────────────────
# Alert handler (threaded)
# Expected payload keys (strings/numbers):
# secret, action in {BUY, ADD, EXIT, HAMMER_BUY, HAMMER_ADD}
# ticker, quantity, entry_price, exit_price, candle_low, candle_close, atr
# ──────────────────────────────
def handle_alert(data: dict):
    try:
        sym = (str(data.get("ticker")) or "").upper()
        action = (str(data.get("action")) or "").upper()
        qty = float(data.get("quantity", 100))
        entry = float(data.get("entry_price", 0) or 0)
        exitp = float(data.get("exit_price", 0) or 0)
        candle_low = float(data.get("candle_low", 0) or 0)
        candle_close= float(data.get("candle_close", 0) or 0)
        atr_val = data.get("atr", None)
        atr_val = float(atr_val) if atr_val not in (None, "", "na") else None

        if not sym:
            log("⚠️ Missing ticker"); return

        log(f"🚀 {action} signal for {sym}")

        if action in ("BUY", "HAMMER_BUY"):
            execute_buy(sym, qty, entry, candle_low, candle_close, atr_val)
        elif action in ("ADD", "HAMMER_ADD"):
            execute_add(sym, qty, entry)
        elif action == "EXIT":
            handle_exit(sym, qty, exitp if exitp > 0 else None)
        else:
            log(f"⚠️ Unknown action: {action}")
    except Exception as e:
        log(f"❌ handle_alert error: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# Webhook endpoint (instant 200)
# ──────────────────────────────
@app.post("/tv")
def tv():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403
    threading.Thread(target=handle_alert, args=(data,), daemon=True).start()
    return jsonify(ok=True)

# ──────────────────────────────
# Ping
# ──────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, service="tv→alpaca", base=ALPACA_BASE_URL)

# ──────────────────────────────
# Run (for local dev; Railway/Gunicorn will import app)
# ──────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))





































