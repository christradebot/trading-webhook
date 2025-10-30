# =========================
# main.py — Athena + Chris 2025
# ITG Scalper + Hammer Logic (v3.3 — sequence + 3-bar wait + retry + partial)
# =========================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
from datetime import datetime, timedelta
import os, time, pytz, threading, traceback

# ──────────────────────────────
# ENV + CLIENT
# ──────────────────────────────
ALPACA_KEY_ID     = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
app = Flask(__name__)
NY = pytz.timezone("America/New_York")

# ──────────────────────────────
# STATE
# ──────────────────────────────
stops, watchers, open_add_tracker, loss_tracker = {}, {}, {}, {}
attempt_tracker, last_signal_price = {}, {}

# Per-symbol sequence & gating
# - first_trade_done:     first entry (of any type) completed
# - scalper_confirmed:    a scalper buy has executed since first trade
# - waiting_for_reversal: set True when first scalper was oversized; forces waiting for a reversal entry
state_tracker = {}

# Pending hammer/engulfing breakout tracking (3-candle window)
# pending_reversal[sym] = {
#   "entry": float, "low": float, "source": "HAMMER_ENGULFING",
#   "candles_waited": int, "max_candles": 3
# }
pending_reversal = {}

lock = threading.Lock()
ENTRY_BUFFER_PCT = 0.002  # 0.2 %

# ──────────────────────────────
# HELPERS
# ──────────────────────────────
def log(msg): print(f"{datetime.now().strftime('%H:%M:%S')} | {msg}", flush=True)

def round_tick(px): return round(px, 4) if px < 1 else round(px, 2)

def latest_bid_ask(sym):
    try:
        q = api.get_latest_quote(sym)
        return float(q.bidprice or 0), float(q.askprice or 0)
    except Exception: return 0, 0

def last_trade_price(sym):
    bid, ask = latest_bid_ask(sym)
    if bid > 0: return bid
    try:
        t = api.get_latest_trade(sym)
        return float(getattr(t, "price", 0.0) or 0.0)
    except Exception: return 0

def safe_qty(sym):
    try: return float(api.get_position(sym).qty)
    except Exception: return 0

def avg_entry_price(sym):
    try: return float(api.get_position(sym).avg_entry_price)
    except Exception: return 0

def cancel_all(sym):
    try:
        for o in api.list_orders(status="open", symbols=[sym]):
            api.cancel_order(o.id)
    except Exception: pass

def entry_trigger_passed(high_price):
    try:
        return last_trade_price_sym >= high_price  # not used directly; kept for reference
    except: return False

# ──────────────────────────────
# RANGE / STOP / LOSS
# ──────────────────────────────
def within_vol_window():
    now = datetime.now(NY).time()
    return datetime.strptime("09:30","%H:%M").time() <= now <= datetime.strptime("09:45","%H:%M").time()

def get_stop(entry_price, signal_low):
    guard = entry_price * 0.03 if within_vol_window() else 0
    return round_tick(min(signal_low, entry_price - guard))

def valid_candle_range(close_p, low_p):
    rng = (close_p - low_p) / close_p * 100 if close_p else 0
    log(f"🔎 Range low→close {rng:.2f}%")
    return rng <= 11

def record_loss(sym):
    with lock:
        loss_tracker[sym] = loss_tracker.get(sym, 0) + 1
        if loss_tracker[sym] >= 2:
            log(f"🚫 {sym} locked after 2 losses")

def can_trade(sym): return loss_tracker.get(sym, 0) < 2

def init_state(sym):
    if sym not in state_tracker:
        state_tracker[sym] = {
            "first_trade_done": False,
            "scalper_confirmed": False,
            "waiting_for_reversal": False
        }

# ──────────────────────────────
# ORDERS / PnL
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
# EXIT MANAGER
# ──────────────────────────────
def managed_exit(sym, qty_hint, target_price=None, mark_stop_loss=False, source="GENERIC"):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0: return
        px = round_tick(target_price or 0)
        if px <= 0:
            bid, ask = latest_bid_ask(sym)
            px = round_tick(bid or ask)
        cancel_all(sym)
        submit_limit("sell", sym, qty, px)
        time.sleep(6)
        step = 0.0005 if px < 1 else 0.02
        while safe_qty(sym) > 0:
            px = round_tick(px - step)
            cancel_all(sym)
            submit_limit("sell", sym, safe_qty(sym), px)
            time.sleep(2)
        if safe_qty(sym) <= 0:
            update_pnl(sym, px, source)
            with lock:
                stops.pop(sym, None)
                open_add_tracker.pop(sym, None)
            if mark_stop_loss: record_loss(sym)
    except Exception as e:
        log(f"❌ managed_exit {sym}: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# STOP WATCHER
# ──────────────────────────────
def stop_watcher(sym, source):
    log(f"👀 Stop watcher for {sym} ({source})")
    while True:
        time.sleep(3)
        info = stops.get(sym)
        if not info or safe_qty(sym) <= 0:
            break
        stop_price = info["stop"]
        last = last_trade_price(sym)
        if last and last <= stop_price:
            log(f"🛑 Stop hit {sym} ({source}) last {last} ≤ {stop_price}")
            managed_exit(sym, safe_qty(sym), stop_price, True, source)
            break

def ensure_watcher(sym, source):
    with lock:
        if sym in watchers and watchers[sym].is_alive(): return
        t = threading.Thread(target=stop_watcher, args=(sym, source), daemon=True)
        watchers[sym] = t; t.start()

# ──────────────────────────────
# SIGNAL PRICE ATTEMPT RESET
# ──────────────────────────────
def reset_attempt_if_new_signal(sym, price):
    """Reset retry counter when a new signal price arrives."""
    last_px = last_signal_price.get(sym)
    if last_px is None or abs(last_px - price) > 1e-6:
        attempt_tracker[sym] = 0
        last_signal_price[sym] = price
        log(f"🔄 Reset attempt tracker for {sym} (new signal price {price})")

# ──────────────────────────────
# PENDING REVERSAL MANAGEMENT (3-CANDLE WINDOW)
# ──────────────────────────────
def check_pending_reversal(sym):
    """On any new alert for this symbol, try to trigger or progress the 3-candle wait."""
    if sym not in pending_reversal:
        return
    pr = pending_reversal[sym]
    entry = pr["entry"]
    last = last_trade_price(sym)
    if last >= entry:
        log(f"💪 Break of high hit during wait for {sym} — executing stored reversal BUY")
        # Place buy using the stored entry/low/source with retry/partial logic:
        _place_entry_with_retry(sym, pr["qty"], entry, pr["low"], pr["source"])
        # Clear pending and sequence gates updated inside _place_entry_with_retry on fill
        pending_reversal.pop(sym, None)
        return
    # Not broken yet → advance window
    pr["candles_waited"] += 1
    left = pr["max_candles"] - pr["candles_waited"]
    if pr["candles_waited"] >= pr["max_candles"]:
        log(f"❌ High not broken after {pr['max_candles']} candles — {sym} reversal setup expired")
        pending_reversal.pop(sym, None)
    else:
        log(f"⏳ Waiting for high break on {sym} reversal ({pr['candles_waited']}/{pr['max_candles']}) — {left} candles left")

# ──────────────────────────────
# CORE ENTRY PLACER (RETRY + PARTIAL)
# ──────────────────────────────
def _place_entry_with_retry(sym, qty, entry, low, source):
    # Price chase guard
    last_px = last_trade_price(sym)
    if last_px > entry:
        log(f"⚠️ {sym} ({source}) skipped — price {last_px:.4f} > entry {entry:.4f}")
        return

    reset_attempt_if_new_signal(sym, entry)
    attempts = attempt_tracker.get(sym, 0)
    if attempts >= 3:
        log(f"⚠️ {sym} ({source}) max retry reached — skipping")
        return

    stop = get_stop(entry, low)

    log(f"🟢 BUY {sym} ({source}) @ {entry} | Stop {stop} (Attempt {attempts+1})")
    submit_limit("buy", sym, qty, entry)
    attempt_tracker[sym] = attempts + 1

    time.sleep(5)
    filled_qty = safe_qty(sym)
    if filled_qty < qty:
        remaining = qty - filled_qty
        if attempts < 2 and remaining > 0:
            log(f"🟡 Partial fill for {sym}: {filled_qty}/{qty} — retrying remaining {remaining}")
            attempt_tracker[sym] = attempts + 1
            submit_limit("buy", sym, remaining, entry)
        else:
            log(f"✅ Partial accepted for {sym}: {filled_qty}/{qty}; cancelling rest")
            cancel_all(sym)

    # If we have a position, set stops and update state
    if safe_qty(sym) > 0:
        with lock:
            stops[sym] = {"stop": stop, "entry": entry}
        ensure_watcher(sym, source)
        # Sequence transitions
        init_state(sym)
        state = state_tracker[sym]
        if not state["first_trade_done"]:
            state["first_trade_done"] = True
            # If first trade is Scalper, that confirms immediately
            if source.upper() == "ITG_SCALPER":
                state["scalper_confirmed"] = True
            state["waiting_for_reversal"] = False
            log(f"⚙️ State updated {sym}: first_trade_done={state['first_trade_done']}, "
                f"scalper_confirmed={state['scalper_confirmed']}, waiting_for_reversal={state['waiting_for_reversal']}")

# ──────────────────────────────
# ENTRY EXECUTION (PUBLIC)
# ──────────────────────────────
def execute_buy(sym, qty, high, low, close, source):
    """Handles both Hammer/Engulfing BUY and Scalper BUY logic with sequence rules."""
    init_state(sym)
    state = state_tracker[sym]

    # Gate: loss lockout & already in position
    if not can_trade(sym):
        return
    if safe_qty(sym) > 0:
        return

    src = source.upper()

    # Range validation — special handling for the first scalper being oversized
    if not valid_candle_range(close, low):
        if not state["first_trade_done"] and src == "ITG_SCALPER":
            state["waiting_for_reversal"] = True
            log(f"🧱 {sym} first Scalper oversized (>11%) — waiting_for_reversal=True (hammer/engulfing only next)")
        return

    # Sequence gating
    if state["first_trade_done"] and src == "HAMMER_ENGULFING" and not state["scalper_confirmed"]:
        log(f"🔒 {sym} hammer blocked — waiting for Scalper confirmation after first trade")
        return

    if not state["first_trade_done"] and state["waiting_for_reversal"] and src == "ITG_SCALPER":
        log(f"🔒 {sym} first Scalper blocked (oversized earlier) — awaiting hammer/engulfing")
        return

    # Determine entry
    if src == "ITG_SCALPER":
        entry = round_tick(close)
        trigger_txt = "close of signal candle"
        log(f"💪 {trigger_txt} confirmed for {sym} ({source})")
        _place_entry_with_retry(sym, qty, entry, low, source)
        return

    # HAMMER_ENGULFING path
    entry = round_tick(high * (1 + ENTRY_BUFFER_PCT))
    last = last_trade_price(sym)

    if last >= entry:
        log(f"💪 Break of high confirmed for {sym} ({source}) — executing immediately")
        _place_entry_with_retry(sym, qty, entry, low, source)
        return

    # Not yet broken — start/advance 3-candle wait
    if sym not in pending_reversal:
        pending_reversal[sym] = {
            "entry": entry,
            "low": low,
            "source": "HAMMER_ENGULFING",
            "qty": qty,
            "candles_waited": 0,
            "max_candles": 3
        }
        log(f"⏳ {sym} hammer/engulfing set — waiting up to 3 candles for high break (entry {entry})")
    else:
        # If a new hammer produces a *different* entry, reset the window to the new one
        if abs(pending_reversal[sym]["entry"] - entry) > 1e-6:
            pending_reversal[sym] = {
                "entry": entry,
                "low": low,
                "source": "HAMMER_ENGULFING",
                "qty": qty,
                "candles_waited": 0,
                "max_candles": 3
            }
            log(f"🔄 {sym} new hammer/engulfing updated — reset 3-candle wait (entry {entry})")

def execute_add(sym, qty, high, low, close, source):
    """Add logic with same retry + partial and sequence gating."""
    init_state(sym)
    state = state_tracker[sym]

    if safe_qty(sym) <= 0 or open_add_tracker.get(sym):
        return
    if not valid_candle_range(close, low):
        return

    src = source.upper()
    if src == "HAMMER_ENGULFING" and not state["scalper_confirmed"]:
        log(f"🔒 {sym} ADD via hammer blocked — requires Scalper confirmation")
        return

    entry = round_tick(high * (1 + ENTRY_BUFFER_PCT))
    last = last_trade_price(sym)
    if last > entry:
        log(f"⚠️ {sym} ADD skipped — price {last:.4f} > entry {entry:.4f}")
        return

    log(f"💪 Break of high confirmed for {sym} ({source}) — executing ADD")
    # Use same core placer (doesn't modify state flags for adds)
    reset_attempt_if_new_signal(f"{sym}_ADD", entry)
    attempts = attempt_tracker.get(f"{sym}_ADD", 0)
    if attempts >= 3:
        log(f"⚠️ {sym} ({source}) max retry reached — skipping adds")
        return

    submit_limit("buy", sym, qty, entry)
    attempt_tracker[f"{sym}_ADD"] = attempts + 1

    time.sleep(5)
    filled_qty = safe_qty(sym)
    if filled_qty <= 0:
        return
    pos_size = filled_qty
    if pos_size < qty:
        remaining = qty - pos_size
        if attempts < 2 and remaining > 0:
            log(f"🟡 Partial ADD {sym}: {pos_size}/{qty} — retrying remaining {remaining}")
            attempt_tracker[f"{sym}_ADD"] = attempts + 1
            submit_limit("buy", sym, remaining, entry)
        else:
            log(f"✅ Partial ADD accepted {sym}: {pos_size}/{qty}; cancelling rest")
            cancel_all(sym)

    open_add_tracker[sym] = True
    ensure_watcher(sym, source)

# ──────────────────────────────
# EXIT / ALERT HANDLER
# ──────────────────────────────
def handle_exit(sym, qty_hint, exit_price, source):
    log(f"🔴 EXIT {sym} ({source}) triggered")
    managed_exit(sym, qty_hint, exit_price, False, source)

def handle_alert(data):
    try:
        sym=(data.get("ticker") or "").upper()
        act=(data.get("action") or "").upper()
        src=data.get("source","GENERIC").upper()
        qty=float(data.get("quantity",100))
        high=float(data.get("signal_high",0))
        low=float(data.get("signal_low",0))
        close=float(data.get("signal_close",0))
        exitp=float(data.get("exit_price",0))

        log(f"🚀 {act} signal for {sym} ({src})")

        # Before handling this candle, progress any pending 3-bar reversal waits
        if sym in pending_reversal and src in ("ITG_SCALPER", "HAMMER_ENGULFING", "GENERIC", "GENERIC_EXIT", "ITG_SCALPER_ADD"):
            check_pending_reversal(sym)

        if act=="BUY":
            execute_buy(sym,qty,high,low,close,src)
            # If a scalper buy executed successfully, mark confirmation
            if src == "ITG_SCALPER" and safe_qty(sym) > 0:
                init_state(sym)
                state_tracker[sym]["scalper_confirmed"] = True
                state_tracker[sym]["waiting_for_reversal"] = False
                log(f"🔓 {sym} scalper confirmed — hammer/engulfing re-enabled")
                # Clear any pending reversal on successful scalper
                pending_reversal.pop(sym, None)
        elif act=="ADD":
            execute_add(sym,qty,high,low,close,src)
        elif act=="EXIT":
            handle_exit(sym,qty,exitp,src)
        else:
            log(f"⚠️ Unknown action {act}")
    except Exception as e:
        log(f"❌ handle_alert {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# WEBHOOK
# ──────────────────────────────
@app.post("/tv")
def tv():
    d=request.get_json(silent=True) or {}
    if d.get("secret")!=WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"),403
    threading.Thread(target=handle_alert,args=(d,),daemon=True).start()
    return jsonify(ok=True)

@app.get("/ping")
def ping():
    return jsonify(ok=True,service="tv→alpaca",base=ALPACA_BASE_URL)

# ──────────────────────────────
# RUN
# ──────────────────────────────
if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",8080)))












































