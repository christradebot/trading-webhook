# ============================
# main.py — Athena + Chris 2025
# ITG Scalper + Validated Hammer / Engulfing (v4.3)
# ============================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
from datetime import datetime, timedelta
import os, time, pytz, threading, traceback

# ──────────────────────────────
# ENV + CLIENT
# ──────────────────────────────
ALPACA_KEY_ID = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
app = Flask(__name__)
NY = pytz.timezone("America/New_York")

# ──────────────────────────────
# PARAMETERS
# ──────────────────────────────
MAX_LOSSES = int(os.getenv("MAX_LOSSES", "2")) # change to 3 if you want later
RANGE_MAX_PCT = 11.0 # low→close max % for a tradable signal

# ──────────────────────────────
# STATE
# ──────────────────────────────
stops = {} # sym -> {"stop": float, "entry": float}
watchers = {} # sym -> Thread
loss_tracker = {} # sym -> int
awaiting_secondary = {} # sym -> True if waiting H/E after oversized scalper
first_trade_done = {} # sym -> True after first successful BUY
trade_state = {} # sym -> "WAITING_STOP_OR_EXIT" | "WAITING_EXIT" | "STOP_HIT"
lock = threading.Lock()

# ──────────────────────────────
# HELPERS
# ──────────────────────────────
def log(msg): print(f"{datetime.now().strftime('%H:%M:%S')} | {msg}", flush=True)
def round_tick(px): return round(px, 4) if px < 1 else round(px, 2)

def latest_bid_ask(sym):
    try:
        q = api.get_latest_quote(sym)
        return float(q.bidprice or 0), float(q.askprice or 0)
    except Exception:
        return 0.0, 0.0

def last_trade_price(sym):
    try:
        t = api.get_latest_trade(sym)
        return float(getattr(t, "price", 0.0) or 0.0)
    except Exception:
        return 0.0

def safe_qty(sym):
    try:
        return float(api.get_position(sym).qty)
    except Exception:
        return 0.0

def avg_entry_price(sym):
    try:
        return float(api.get_position(sym).avg_entry_price)
    except Exception:
        return 0.0

def cancel_all(sym):
    try:
        for o in api.list_orders(status="open", symbols=[sym]):
            api.cancel_order(o.id)
    except Exception:
        pass

def rng_low_to_close_pct(close_p, low_p):
    return (close_p - low_p) / close_p * 100.0 if close_p else 0.0

def can_trade(sym):
    return loss_tracker.get(sym, 0) < MAX_LOSSES

def record_loss(sym):
    with lock:
        loss_tracker[sym] = loss_tracker.get(sym, 0) + 1
        if loss_tracker[sym] >= MAX_LOSSES:
            log(f"🚫 {sym} locked after {loss_tracker[sym]} losses")

def get_stop(entry_price, signal_low):
    # Stop is the low of the signal candle (guard against weird inputs)
    stop = min(signal_low, entry_price * 0.97)
    return round_tick(stop)

# ──────────────────────────────
# ORDER + PNL
# ──────────────────────────────
def submit_limit(side, sym, qty, px):
    try:
        api.submit_order(
            symbol=sym, qty=int(qty), side=side, type="limit",
            limit_price=round_tick(px), time_in_force="day", extended_hours=True
        )
        log(f"📥 {side.upper()} LIMIT {sym} @ {round_tick(px)} x{int(qty)}")
    except Exception as e:
        log(f"⚠️ submit_limit {sym}: {e}")

def update_pnl(sym, exit_price, source):
    try:
        avg, qty = avg_entry_price(sym), safe_qty(sym)
        pnl_d = (exit_price - avg) * qty
        pnl_p = ((exit_price / avg) - 1) * 100 if avg > 0 else 0
        log(f"💰 {sym} EXIT ({source}) @ {exit_price:.4f} | PnL ${pnl_d:.2f} ({pnl_p:.2f}%)")
    except Exception:
        log(f"💰 {sym} EXIT ({source}) @ {exit_price}")

# ──────────────────────────────
# EXIT MANAGEMENT (cascading, must-flat)
# ──────────────────────────────
def managed_exit(sym, qty_hint, target_price=None, mark_stop_loss=False, source="GENERIC"):
    try:
        qty = safe_qty(sym) or float(qty_hint or 0)
        if qty <= 0:
            return

        # Clear resting orders first
        cancel_all(sym)

        # Escalate up to ~8 attempts
        for attempt in range(1, 9):
            bid, ask = latest_bid_ask(sym)
            last = last_trade_price(sym)
            px = target_price or bid or last or ask
            if not px or px <= 0:
                time.sleep(0.5)
                continue

            px = round_tick(px)
            submit_limit("sell", sym, qty, px)
            log(f"🔻 EXIT attempt {attempt} for {sym} @ {px} ({source})")
            time.sleep(2.0)

            remaining = safe_qty(sym)
            if remaining <= 0:
                update_pnl(sym, px, source)
                with lock:
                    stops.pop(sym, None)
                    trade_state.pop(sym, None)
                if mark_stop_loss:
                    record_loss(sym)
                return

            # Not filled → cancel & get more aggressive
            cancel_all(sym)
            step = 0.002 if px < 1 else 0.01
            target_price = max(px - step, 0.0001)

        # Final push
        bid, ask = latest_bid_ask(sym)
        last = last_trade_price(sym)
        px = round_tick((min([x for x in [bid, last] if x > 0]) or 0) * 0.997)
        if px and px > 0:
            submit_limit("sell", sym, qty, px)
            log(f"⚠️ EXIT final push for {sym} @ {px} ({source})")
            time.sleep(3)

        remaining = safe_qty(sym)
        if remaining > 0:
            log(f"❗ Could not fully exit {sym}. Still holding {remaining}.")
        else:
            update_pnl(sym, px, source)
            with lock:
                stops.pop(sym, None)
                trade_state.pop(sym, None)
            if mark_stop_loss:
                record_loss(sym)

    except Exception as e:
        log(f"❌ managed_exit {sym}: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# STOP WATCHER (no pre-placed stops; confirm with two reads)
# ──────────────────────────────
def stop_watcher(sym, source):
    log(f"👀 Watching stop for {sym} ({source})")
    hits = 0
    while True:
        time.sleep(0.8)
        info = stops.get(sym)
        if not info or safe_qty(sym) <= 0:
            break
        stop_price = info["stop"]
        last = last_trade_price(sym)
        if not last or last <= 0:
            continue
        if last <= stop_price:
            hits += 1
        else:
            hits = 0
        if hits >= 2:
            with lock:
                trade_state[sym] = "STOP_HIT"
            log(f"🛑 Stop hit {sym} ({source}) last {last} ≤ {stop_price}")
            managed_exit(sym, safe_qty(sym), stop_price, True, source)
            break

def ensure_watcher(sym, source):
    with lock:
        if sym in watchers and watchers[sym].is_alive():
            return
        t = threading.Thread(target=stop_watcher, args=(sym, source), daemon=True)
        watchers[sym] = t
        t.start()

# ──────────────────────────────
# TRADE LOGIC
# ──────────────────────────────
def valid_candle_range(close_p, low_p):
    rng = rng_low_to_close_pct(close_p, low_p)
    log(f"🔎 Range low→close {rng:.2f}%")
    return rng <= RANGE_MAX_PCT

def execute_buy(sym, qty, entry_price, signal_low, source):
    if not can_trade(sym) or safe_qty(sym) > 0:
        log(f"⚠️ Skipping BUY {sym} ({source}) — locked or already in position")
        return
    if not valid_candle_range(entry_price, signal_low):
        log(f"⚠️ Skipping BUY {sym} ({source}) — invalid candle range")
        return

    stop = get_stop(entry_price, signal_low)
    log(f"🟢 BUY {sym} ({source}) @ {entry_price} | Stop (signal low) {stop}")
    submit_limit("buy", sym, qty, entry_price)

    with lock:
        stops[sym] = {"stop": stop, "entry": entry_price}
        trade_state[sym] = "WAITING_STOP_OR_EXIT"
        first_trade_done[sym] = True
    log(f"🟡 {sym} waiting for STOP or EXIT")
    ensure_watcher(sym, source)

def handle_exit(sym, qty_hint, exit_price, source):
    with lock:
        trade_state[sym] = "WAITING_EXIT"
    log(f"🟠 {sym} entering EXIT flow (state=WAITING_EXIT)")
    managed_exit(sym, qty_hint, exit_price, False, source)

# ──────────────────────────────
# HANDLER
# ──────────────────────────────
def handle_alert(data):
    try:
        sym = (data.get("ticker") or "").upper()
        act = (data.get("action") or "").upper()
        src = data.get("source", "GENERIC").upper()
        qty = float(data.get("quantity", 100))
        close_p = float(data.get("signal_close", 0))
        low_p = float(data.get("signal_low", 0))
        exit_p = float(data.get("exit_price", 0))

        state = trade_state.get(sym, "IDLE")
        rng = rng_low_to_close_pct(close_p, low_p) if close_p else 0.0
        log(f"📟 {sym} state={state} | incoming {act} ({src}) | range {rng:.2f}%")

        # EXIT has highest priority
        if act == "EXIT":
            handle_exit(sym, qty, exit_p, src)
            return

        # Normalize sources
        is_scalper = (src == "SCALPER_BUY")
        is_he = (src in {"HAMMER_EMA5", "ENGULFING_EMA5", "HAMMER_ENGULFING_BUY"})

        if act != "BUY":
            log(f"⚠️ Unknown action {act}")
            return

        # FIRST TRADE LOGIC: before first_trade_done → allow Scalper or H/E
        if not first_trade_done.get(sym, False):
            if is_scalper:
                if rng <= RANGE_MAX_PCT:
                    log(f"🟢 SCALPER {sym} valid ({rng:.2f}%) — first trade")
                    awaiting_secondary.pop(sym, None)
                    execute_buy(sym, qty, close_p, low_p, src)
                else:
                    log(f"⚠️ SCALPER {sym} too large → awaiting valid Hammer/Engulfing for FIRST trade")
                    awaiting_secondary[sym] = True
            elif is_he:
                # If first trade and we either are awaiting or not — a valid H/E is allowed
                if awaiting_secondary.get(sym):
                    log(f"🟢 Secondary entry unlocked — Valid {src} {sym} (FIRST trade)")
                    awaiting_secondary.pop(sym, None)
                else:
                    log(f"🟢 Valid {src} {sym} (FIRST trade)")
                execute_buy(sym, qty, close_p, low_p, src)
            else:
                log(f"⚠️ Unknown source '{src}' for first trade BUY")
            return

        # AFTER FIRST TRADE: Scalper-first. If too large, wait for H/E.
        if is_scalper:
            if rng <= RANGE_MAX_PCT:
                log(f"🟢 SCALPER {sym} valid ({rng:.2f}%) — subsequent trade")
                awaiting_secondary.pop(sym, None)
                execute_buy(sym, qty, close_p, low_p, src)
            else:
                log(f"⚠️ SCALPER {sym} too large → awaiting valid Hammer/Engulfing")
                awaiting_secondary[sym] = True
            return

        if is_he:
            if awaiting_secondary.get(sym):
                log(f"🟢 Secondary entry unlocked — Valid {src} {sym} (AFTER first)")
                awaiting_secondary.pop(sym, None)
                execute_buy(sym, qty, close_p, low_p, src)
            else:
                log(f"⚠️ Ignoring {src} for {sym} — need SCALPER first after first trade")
            return

        log(f"⚠️ Unknown source '{src}' for BUY")

    except Exception as e:
        log(f"❌ handle_alert {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# DAILY AUTO-FLAT @ 19:59 ET
# ──────────────────────────────
def eod_liquidator():
    log("⏰ EOD liquidator started (auto-flat before 20:00 ET)")
    while True:
        try:
            now = datetime.now(NY)
            cutoff = now.replace(hour=19, minute=59, second=0, microsecond=0)
            if now >= cutoff:
                # if already past, wait until next day 19:59
                cutoff = (now + timedelta(days=1)).replace(hour=19, minute=59, second=0, microsecond=0)
            sleep_s = (cutoff - now).total_seconds()
            time.sleep(min(max(sleep_s, 1), 60)) # wake up at least once a minute

            # Recompute; trigger when we're in or past the minute
            now = datetime.now(NY)
            if now.hour == 19 and now.minute == 59:
                log("🌙 Daily cleanup: closing all positions before overnight.")
                # Cancel all orders
                try:
                    for o in api.list_orders(status="open"):
                        api.cancel_order(o.id)
                except Exception as e:
                    log(f"EOD cancel error: {e}")

                # Liquidate all open longs
                try:
                    positions = api.list_positions()
                    for p in positions:
                        sym = p.symbol
                        qty = float(p.qty)
                        if qty > 0:
                            managed_exit(sym, qty, None, False, "EOD_FLAT")
                except Exception as e:
                    log(f"EOD liquidate error: {e}")

                time.sleep(5)
        except Exception:
            time.sleep(5)

# ──────────────────────────────
# WEBHOOKS
# ──────────────────────────────
@app.post("/tv")
def tv():
    d = request.get_json(silent=True) or {}
    if d.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403
    threading.Thread(target=handle_alert, args=(d,), daemon=True).start()
    return jsonify(ok=True)

@app.get("/ping")
def ping():
    return jsonify(ok=True, service="tv→alpaca", base=ALPACA_BASE_URL)

# ──────────────────────────────
# RUN
# ──────────────────────────────
if __name__ == "__main__":
    threading.Thread(target=eod_liquidator, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

















































