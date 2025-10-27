# =========================
# main.py — Athena + Chris 2025
# ITG Scalper Bot (Async + Limit-only + Auto Stop Watcher)
# =========================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
from datetime import datetime, timedelta
import os, time, json, traceback, pytz, threading

# ──────────────────────────────
# Environment + API setup
# ──────────────────────────────
ALPACA_KEY_ID     = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
app = Flask(__name__)
NY = pytz.timezone("America/New_York")

# ──────────────────────────────
# State
# ──────────────────────────────
open_add_tracker = {}     # one add per ticker
loss_tracker     = {}     # max two losses per ticker (session)
stops            = {}     # {sym: {"stop": float, "entry": float}}
watchers         = {}     # {sym: threading.Thread}

lock = threading.Lock()

# ──────────────────────────────
# Helpers
# ──────────────────────────────
def log(msg): print(f"{datetime.now().strftime('%H:%M:%S')} | {msg}")

def round_tick(px):
    if px is None:
        return 0
    # Tighter precision for sub-$1 names
    return round(px, 4) if px < 1 else round(px, 2)

def latest_bid_ask(sym):
    try:
        q = api.get_latest_quote(sym)
        # handle 0.0 quotes gracefully
        bid = float(q.bidprice or 0)
        ask = float(q.askprice or 0)
        return bid, ask
    except Exception:
        return 0.0, 0.0

def last_trade_price(sym):
    # Prefer bid if present (for sells), else last trade
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
        return cur > 0 and avg > 0 and cur > avg
    except Exception:
        return False

def cancel_all(sym):
    try:
        for o in api.list_orders(status="open", symbols=[sym]):
            api.cancel_order(o.id)
    except Exception:
        pass

def update_pnl(sym, price):
    log(f"💰 Recorded exit for {sym} @ {price}")

# ──────────────────────────────
# Time / Filters
# ──────────────────────────────
def within_vol_window():
    now = datetime.now(NY).time()
    return datetime.strptime("09:30","%H:%M").time() <= now <= datetime.strptime("09:45","%H:%M").time()

def get_stop(entry_price, signal_low):
    # Wider guard only during 09:30–09:45 ET
    if within_vol_window():
        guard = entry_price * 0.03  # ~3% buffer
        stop = min(signal_low, entry_price - guard)
    else:
        stop = signal_low
    return round_tick(stop)

def valid_candle_range(close_p, low_p):
    rng = (close_p - low_p) / close_p * 100 if close_p else 0
    log(f"🔎 Entry range (low→close): {rng:.2f}%")
    return rng <= 10.0

def record_loss(sym):
    with lock:
        loss_tracker[sym] = loss_tracker.get(sym, 0) + 1
        if loss_tracker[sym] >= 2:
            log(f"🚫 {sym} locked out after 2 losses.")

def can_trade(sym):
    return loss_tracker.get(sym, 0) < 2

# ──────────────────────────────
# Order submitters (limit-only)
# ──────────────────────────────
def submit_limit(side, sym, qty, px):
    try:
        api.submit_order(
            symbol=sym,
            qty=int(qty),
            side=side,
            type="limit",
            time_in_force="day",
            limit_price=round_tick(px),
            extended_hours=True  # eligible outside RTH; ignored during RTH
        )
        log(f"📥 {side.upper()} LIMIT {sym} @ {round_tick(px)} x{int(qty)}")
    except Exception as e:
        log(f"⚠️ submit_limit {sym}: {e}")

# ──────────────────────────────
# Managed Exit (limit-only, aggressive fallback)
# ──────────────────────────────
def managed_exit(sym, qty_hint, target_price=None, mark_stop_loss=False):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0:
            return

        limit_price = round_tick(target_price) if target_price else 0
        if limit_price <= 0:
            bid, ask = latest_bid_ask(sym)
            limit_price = round_tick(bid or ask)
        if limit_price <= 0:
            log(f"⚠️ No valid exit price for {sym}, skipping.")
            return

        log(f"🟣 Exit target for {sym} @ {limit_price}")
        cancel_all(sym)
        submit_limit("sell", sym, qty, limit_price)
        time.sleep(8)

        # Aggressive fallback loop (still limit)
        if safe_qty(sym) > 0:
            # small step for pennies, slightly bigger for higher prices
            step = 0.0005 if limit_price < 1 else 0.02
            end_time = datetime.now(NY) + timedelta(minutes=5)
            px = limit_price
            while datetime.now(NY) < end_time and safe_qty(sym) > 0:
                px = round_tick(px - step)
                cancel_all(sym)
                submit_limit("sell", sym, safe_qty(sym), px)
                time.sleep(3)

        # Final status
        if safe_qty(sym) <= 0:
            update_pnl(sym, limit_price)
            log(f"✅ Closed {sym}")
            with lock:
                stops.pop(sym, None)
                open_add_tracker.pop(sym, None)
            if mark_stop_loss:
                record_loss(sym)
        else:
            log(f"⚠️ Could not close {sym} fully.")

    except Exception as e:
        log(f"❌ managed_exit {sym}: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# Background Stop Watcher
# ──────────────────────────────
def stop_watcher(sym):
    log(f"👀 Stop watcher started for {sym}")
    try:
        # Poll every 5 seconds while position exists and stop is set
        while True:
            time.sleep(5)
            with lock:
                info = stops.get(sym)
            if info is None:
                # no stop tracked
                break
            if safe_qty(sym) <= 0:
                # flat, stop no longer relevant
                with lock:
                    stops.pop(sym, None)
                break

            stop_price = info["stop"]
            last = last_trade_price(sym)
            if last <= 0:
                continue

            if last <= stop_price:
                log(f"🛑 Stop hit for {sym}: last {last} <= stop {stop_price}")
                # Fire managed exit at stop (limit-only), track as a loss
                managed_exit(sym, safe_qty(sym), target_price=stop_price, mark_stop_loss=True)
                break
    except Exception as e:
        log(f"❌ stop_watcher {sym}: {e}\n{traceback.format_exc()}")
    finally:
        log(f"🧹 Stop watcher ended for {sym}")

def ensure_watcher(sym):
    with lock:
        if sym in watchers and watchers[sym].is_alive():
            return
        t = threading.Thread(target=stop_watcher, args=(sym,), daemon=True)
        watchers[sym] = t
        t.start()

# ──────────────────────────────
# Trade actions
# ──────────────────────────────
def execute_buy(sym, qty, entry_price, signal_low):
    if not can_trade(sym):
        log(f"🚫 Skipping {sym}: reached loss limit.")
        return
    if safe_qty(sym) > 0:
        log(f"⏩ Already in position {sym}, skip BUY.")
        return
    if not valid_candle_range(entry_price, signal_low):
        log(f"⚠️ Skipped {sym}: low→close > 10%.")
        return

    stop_price = get_stop(entry_price, signal_low)
    log(f"🟢 BUY {sym} @ {round_tick(entry_price)} | Stop {round_tick(stop_price)} (candle low)")
    submit_limit("buy", sym, qty, entry_price)

    # Save stop + entry and launch watcher
    with lock:
        stops[sym] = {"stop": stop_price, "entry": round_tick(entry_price)}
    ensure_watcher(sym)

def execute_add(sym, qty, entry_price):
    if safe_qty(sym) <= 0:
        log(f"⚠️ No open position for {sym}, skip ADD.")
        return
    if open_add_tracker.get(sym):
        log(f"⚠️ Add already used for {sym}.")
        return
    if not in_profit(sym):
        log(f"⚠️ Position not in profit for {sym}, skip ADD.")
        return

    log(f"➕ ADD {sym} @ {round_tick(entry_price)}")
    submit_limit("buy", sym, qty, entry_price)
    open_add_tracker[sym] = True

def handle_exit(sym, qty_hint, exit_price):
    # Manual EXIT via webhook: use given price, do not mark as loss automatically
    managed_exit(sym, qty_hint, target_price=exit_price, mark_stop_loss=False)

# ──────────────────────────────
# Alert handler (threaded)
# ──────────────────────────────
def handle_alert(data):
    try:
        sym         = (data.get("ticker") or "").upper()
        action      = (data.get("action") or "").upper()
        qty         = float(data.get("quantity", 100))
        entry       = float(data.get("entry_price", 0))
        exitp       = float(data.get("exit_price", 0))
        signal_low  = float(data.get("signal_low", 0))

        log(f"🚀 {action} signal for {sym}")

        if action == "BUY":
            execute_buy(sym, qty, entry, signal_low)
        elif action == "ADD":
            execute_add(sym, qty, entry)
        elif action == "EXIT":
            handle_exit(sym, qty, exitp)
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

































