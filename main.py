# ============================
# main.py — Athena + Chris 2025
# ITG Scalper + Validated Hammer/Engulfing (v4.5)
# ============================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
from datetime import datetime
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
# STATE
# ──────────────────────────────
stops, watchers, loss_tracker = {}, {}, {}
awaiting_secondary = {} # after an oversized SCALPER_BUY, wait for hammer/engulfing
first_trade_done = {} # per-symbol session flag: False until the very first trade is taken
lock = threading.Lock()

# ──────────────────────────────
# HELPERS
# ──────────────────────────────
def log(msg):
    print(f"{datetime.now().strftime('%H:%M:%S')} | {msg}", flush=True)

def round_tick(px):
    try:
        return round(px, 4) if px < 1 else round(px, 2)
    except Exception:
        return px

def get_float(x, default=0.0):
    try:
        if x is None or (isinstance(x, str) and x.strip() == ""):
            return default
        return float(x)
    except Exception:
        return default

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

# ──────────────────────────────
# STOP / LOSS
# ──────────────────────────────
def get_stop(entry_price, signal_low):
    """Stop is ALWAYS the low of the signal candle."""
    return round_tick(signal_low)

def record_loss(sym):
    with lock:
        loss_tracker[sym] = loss_tracker.get(sym, 0) + 1
        if loss_tracker[sym] >= 2:
            log(f"🚫 {sym} locked after 2 losses")

def can_trade(sym):
    return loss_tracker.get(sym, 0) < 2

# ──────────────────────────────
# ORDER + PnL
# ──────────────────────────────
def submit_limit(side, sym, qty, px):
    try:
        api.submit_order(
            symbol=sym,
            qty=int(qty),
            side=side,
            type="limit",
            limit_price=round_tick(px),
            time_in_force="day",
            extended_hours=True
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
# EXIT MANAGEMENT
# ──────────────────────────────
def managed_exit(sym, qty_hint, target_price=None, mark_stop_loss=False, source="GENERIC"):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0:
            return
        bid, ask = latest_bid_ask(sym)
        px = round_tick(target_price or bid or ask)
        if px <= 0:
            return
        cancel_all(sym)
        submit_limit("sell", sym, qty, px)
        time.sleep(5)
        if safe_qty(sym) <= 0:
            update_pnl(sym, px, source)
            with lock:
                stops.pop(sym, None)
            if mark_stop_loss:
                record_loss(sym)
    except Exception as e:
        log(f"❌ managed_exit {sym}: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# STOP WATCHER (pre-market safe; polls Alpaca live prices)
# ──────────────────────────────
def stop_watcher(sym, source):
    log(f"👀 Watching stop for {sym} ({source})")
    while True:
        time.sleep(2) # poll every 2s
        info = stops.get(sym)
        if not info or safe_qty(sym) <= 0:
            break

        stop_price = info["stop"]

        # Live price from Alpaca (trade first, else quote)
        last = last_trade_price(sym)
        bid, ask = latest_bid_ask(sym)
        live = last or bid or ask
        if live <= 0:
            continue

        if live <= stop_price:
            log(f"🛑 Stop triggered for {sym} ({source}) — live {live} ≤ stop {stop_price}")
            # Tiny buffer to help fill a limit in pre-market
            sell_px = round_tick(stop_price * 0.999)
            managed_exit(sym, safe_qty(sym), sell_px, True, source)
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
    rng = (close_p - low_p) / close_p * 100 if close_p else 0
    log(f"🔎 Range low→close {rng:.2f}%")
    return rng <= 11, rng

def execute_buy(sym, qty, entry_price, signal_low, source):
    if not can_trade(sym) or safe_qty(sym) > 0:
        log(f"⚠️ Skipping BUY {sym} ({source}) — locked or already in position")
        return
    ok, rng = valid_candle_range(entry_price, signal_low)
    if not ok:
        log(f"⚠️ Skipping BUY {sym} ({source}) — invalid candle range {rng:.2f}%")
        return

    stop = get_stop(entry_price, signal_low) # always signal low
    log(f"🟢 BUY {sym} ({source}) @ {entry_price} | Stop (signal low) {stop}")
    submit_limit("buy", sym, qty, entry_price)
    with lock:
        stops[sym] = {"stop": stop, "entry": entry_price}
    ensure_watcher(sym, source)

# ──────────────────────────────
# ALERT HANDLER (session-aware; no body-break logic)
# ──────────────────────────────
BUY_SOURCES_SCALPER = {"SCALPER_BUY"}
BUY_SOURCES_HAM_ENG = {"HAMMER_EMA5", "ENGULFING_EMA5"}

def handle_alert(data):
    try:
        sym = (data.get("ticker") or "").upper()
        act = (data.get("action") or "").upper() # "BUY"/"ADD"/"EXIT"
        src = (data.get("source") or "GENERIC").upper()
        qty = get_float(data.get("quantity", 100))
        close_p = get_float(data.get("signal_close", 0))
        low_p = get_float(data.get("signal_low", 0))
        exit_p = get_float(data.get("exit_price", 0))

        if not sym:
            log("⚠️ Missing ticker; ignoring alert")
            return

        # If action is blank but source implies a buy, treat as BUY
        if act not in {"BUY", "ADD", "EXIT"} and (src in BUY_SOURCES_SCALPER or src in BUY_SOURCES_HAM_ENG):
            act = "BUY"

        # Log context
        if act == "EXIT":
            log(f"🚀 EXIT signal for {sym} ({src})")
        else:
            rng = (close_p - low_p) / close_p * 100 if close_p else 0
            log(f"🚀 {act} signal for {sym} ({src}) | range {rng:.2f}% | first_trade_done={first_trade_done.get(sym, False)}")

        # ─── EXIT ───
        if act == "EXIT":
            # After any exit, we DO NOT reset first_trade_done.
            # Session stays in "scalper-first" mode permanently after the first trade of the day.
            managed_exit(sym= sym, qty_hint= qty, target_price= exit_p, mark_stop_loss= False, source= src)
            # Clear awaiting_secondary just to be safe for next cycle
            awaiting_secondary.pop(sym, None)
            return

        # ─── BUY/ADD paths ───
        # Normalize ADD to BUY behavior (scale-ins treated like entries)
        if act in {"BUY", "ADD"}:
            # 1) BEFORE FIRST TRADE: allow any of the three (Scalper or Hammer/Engulfing)
            if not first_trade_done.get(sym, False):
                if src in BUY_SOURCES_SCALPER:
                    ok, _ = valid_candle_range(close_p, low_p)
                    if ok:
                        execute_buy(sym, qty, close_p, low_p, src)
                        first_trade_done[sym] = True
                        awaiting_secondary.pop(sym, None)
                    else:
                        log(f"⚠️ SCALPER {sym} too large → awaiting valid Hammer/Engulfing for FIRST trade")
                        awaiting_secondary[sym] = True
                elif src in BUY_SOURCES_HAM_ENG:
                    # If we were awaiting due to oversized scalper, or even if not, first trade can be hammer/engulfing
                    execute_buy(sym, qty, close_p, low_p, src)
                    first_trade_done[sym] = True
                    awaiting_secondary.pop(sym, None)
                else:
                    log(f"⚠️ Unknown source '{src}' for first trade BUY")
                return

            # 2) AFTER FIRST TRADE: must start with SCALPER; hammer/engulfing only as secondary
            else:
                if src in BUY_SOURCES_SCALPER:
                    ok, _ = valid_candle_range(close_p, low_p)
                    if ok:
                        execute_buy(sym, qty, close_p, low_p, src)
                        awaiting_secondary.pop(sym, None)
                    else:
                        log(f"⚠️ SCALPER {sym} too large → awaiting valid Hammer/Engulfing (secondary)")
                        awaiting_secondary[sym] = True
                    return

                if src in BUY_SOURCES_HAM_ENG:
                    if awaiting_secondary.get(sym):
                        log(f"🟢 Secondary entry unlocked — {src} for {sym}")
                        awaiting_secondary.pop(sym, None)
                        execute_buy(sym, qty, close_p, low_p, src)
                    else:
                        log(f"⚠️ Ignoring {src} for {sym} — post-first trade requires SCALPER first")
                    return

                log(f"⚠️ Unknown source '{src}' for BUY")
                return

        # Fallback
        log(f"⚠️ Unknown action/source combo: action={act} source={src}")

    except Exception as e:
        log(f"❌ handle_alert {e}\n{traceback.format_exc()}")

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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

















































