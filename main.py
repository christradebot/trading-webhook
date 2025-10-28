# =========================
# main.py — Athena + Chris 2025
# ITG Scalper Bot (Lean) — Limit-only, Alpaca price feed, Stop watcher, PnL
# =========================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os, time, json, threading, traceback, math

# ──────────────────────────────
# Env + API
# ──────────────────────────────
ALPACA_KEY_ID     = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
NY  = ZoneInfo("America/New_York")

app = Flask(__name__)

# ──────────────────────────────
# Session State (in-memory)
# ──────────────────────────────
stops      = {}    # {SYM: {"stop": float, "entry": float}}
loss_count = {}    # {SYM: int}  max 2
add_used   = {}    # {SYM: bool}
watchers   = {}    # {SYM: Thread}
lock       = threading.Lock()

# ──────────────────────────────
# Helpers
# ──────────────────────────────
def ts(): return datetime.now(NY).strftime("%H:%M:%S")
def log(msg): print(f"{ts()} | {msg}", flush=True)

def round_tick(p: float) -> float:
    if p is None: return 0.0
    p = float(p)
    step = 0.01 if p >= 1 else (0.001 if p >= 0.1 else 0.0001)
    return float(f"{math.floor(p/step)*step:.6f}")

def latest_bid_ask_trade(sym):
    """Use Alpaca price feed (quotes + last trade) robustly."""
    bid = ask = last = None
    try:
        q = api.get_latest_quote(sym)
        bid = float(q.bidprice) if q and q.bidprice else None
        ask = float(q.askprice) if q and q.askprice else None
    except Exception:
        pass
    try:
        t = api.get_latest_trade(sym)
        last = float(t.price) if t and t.price else None
    except Exception:
        last = None
    return bid, ask, last

def last_trade_price(sym) -> float:
    bid, ask, last = latest_bid_ask_trade(sym)
    return float(last or bid or ask or 0.0)

def safe_qty(sym) -> float:
    try:
        pos = api.get_position(sym)
        return float(pos.qty)
    except Exception:
        return 0.0

def avg_entry(sym) -> float:
    try:
        pos = api.get_position(sym)
        return float(pos.avg_entry_price)
    except Exception:
        return 0.0

def in_profit(sym) -> bool:
    cur = last_trade_price(sym)
    avg = avg_entry(sym)
    return cur > 0 and avg > 0 and cur > avg

def cancel_open_orders(sym):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == sym:
                api.cancel_order(o.id)
        log(f"🧹 Cancelled open orders for {sym}")
    except Exception as e:
        log(f"⚠️ cancel_open_orders({sym}): {e}")

def submit_limit(side, sym, qty, price):
    price = round_tick(price)
    try:
        api.submit_order(
            symbol=sym,
            side=side,
            qty=str(int(qty)),
            type="limit",
            limit_price=str(price),
            time_in_force="day",
            extended_hours=True  # valid RTH + extended
        )
        log(f"📥 {side.upper()} LIMIT {sym} @{price} x{int(qty)}")
        return True
    except Exception as e:
        log(f"❌ {side.upper()} limit error {sym} @{price}: {e}")
        return False

# ──────────────────────────────
# Guards / Stops
# ──────────────────────────────
def opening_vol_window() -> bool:
    now = datetime.now(NY)
    start = now.replace(hour=9, minute=30, second=0, microsecond=0)
    end   = now.replace(hour=9, minute=45, second=0, microsecond=0)
    return start <= now <= end

def entry_range_ok(candle_low, candle_close, max_pct=10.0) -> bool:
    try:
        low   = float(candle_low)
        close = float(candle_close)
        if close <= 0: return False
        rng = ((close - low) / close) * 100.0
        log(f"🔎 Range low→close = {rng:.2f}%")
        return rng <= max_pct
    except Exception:
        return True  # if no fields, allow (alert should usually send both)

def compute_stop(entry_close, signal_low) -> float:
    """Base stop on signal candle low; during 09:30–09:45 add 3% guard."""
    entry_close = float(entry_close)
    signal_low  = float(signal_low)
    if opening_vol_window():
        guard = entry_close * 0.03
        return round_tick(min(signal_low, entry_close - guard))
    return round_tick(signal_low)

def record_loss(sym):
    with lock:
        loss_count[sym] = loss_count.get(sym, 0) + 1
        if loss_count[sym] >= 2:
            log(f"🚫 {sym} locked after 2 losses")

def can_trade(sym) -> bool:
    return loss_count.get(sym, 0) < 2

# ──────────────────────────────
# Aggressive limit-only exit loop
# ──────────────────────────────
def aggressive_limit_exit(sym, start_limit, reason="EXIT", max_tries=30, pause=2.0):
    tries = 0
    px = round_tick(start_limit)

    # choose tick from current price context
    _, _, last = latest_bid_ask_trade(sym)
    tick = 0.01 if (last or 1) >= 1 else (0.001 if (last or 0.1) >= 0.1 else 0.0001)

    while tries < max_tries and safe_qty(sym) > 0:
        cancel_open_orders(sym)
        bid, ask, last = latest_bid_ask_trade(sym)
        ref = bid or last or ask or px
        # sit one tick under current bid to get filled
        px = max(round_tick(ref - tick), tick)
        submit_limit("sell", sym, safe_qty(sym), px)
        tries += 1
        log(f"⏱ Aggressive EXIT {sym} {tries}/{max_tries} @ {px}")
        time.sleep(pause)

    # Log PnL even if partially filled (best effort)
    qty_closed = "all" if safe_qty(sym) <= 0 else "partial"
    _log_pnl(sym, px, reason, qty_closed)

def _log_pnl(sym, exit_price, reason, qty_closed="all"):
    a = avg_entry(sym)
    if a > 0:
        pnl_pct = ((float(exit_price) / a) - 1.0) * 100.0
        log(f"💰 {sym} EXIT @{round_tick(exit_price)} (avg {round_tick(a)}) | PnL {pnl_pct:.2f}% | {reason} | {qty_closed}")

# ──────────────────────────────
# Stop watcher (uses Alpaca price feed)
# ──────────────────────────────
def ensure_watcher(sym):
    with lock:
        if sym in watchers and watchers[sym].is_alive():
            return
        t = threading.Thread(target=_watch_stop, args=(sym,), daemon=True)
        watchers[sym] = t
        t.start()

def _watch_stop(sym):
    log(f"👀 Stop watcher ON for {sym}")
    try:
        while True:
            time.sleep(1.0)
            with lock:
                info = stops.get(sym)
            if info is None:
                return
            if safe_qty(sym) <= 0:
                with lock:
                    stops.pop(sym, None)
                    add_used[sym] = False
                return

            stop_lvl = float(info["stop"])
            last = last_trade_price(sym)
            if last <= 0:
                continue

            if last <= stop_lvl:
                log(f"🛑 STOP HIT {sym}: last {last:.6f} ≤ {stop_lvl:.6f}")
                cancel_open_orders(sym)
                # Try stop level first
                filled = submit_limit("sell", sym, safe_qty(sym), stop_lvl)
                time.sleep(3)
                if safe_qty(sym) > 0:
                    aggressive_limit_exit(sym, stop_lvl, reason="STOP")
                else:
                    _log_pnl(sym, stop_lvl, "STOP", "all")
                    record_loss(sym)
                with lock:
                    stops.pop(sym, None)
                return
    except Exception as e:
        log(f"❌ watcher {sym}: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# Core actions (driven by alerts)
# ──────────────────────────────
def do_buy(sym, qty, entry_price, candle_low, candle_close):
    if not can_trade(sym):
        log(f"🚫 BUY blocked {sym}: loss limit")
        return
    if safe_qty(sym) > 0:
        log(f"ℹ️ BUY ignored {sym}: already in position")
        return
    if candle_low is not None and candle_close is not None:
        if not entry_range_ok(candle_low, candle_close, 10.0):
            log(f"🚫 BUY blocked {sym}: low→close > 10%")
            return

    # Place entry
    if not submit_limit("buy", sym, qty, entry_price):
        return

    # Compute & arm stop
    if candle_low is not None and candle_close is not None:
        stop = compute_stop(candle_close, candle_low)
        with lock:
            stops[sym] = {"stop": stop, "entry": round_tick(entry_price)}
        log(f"🔒 STOP armed {sym} @ {stop}")
        ensure_watcher(sym)
    else:
        log(f"⚠️ No candle_low/close supplied; stop not armed for {sym}")

def do_add(sym, qty, entry_price):
    if safe_qty(sym) <= 0:
        log(f"ℹ️ ADD fallback→BUY {sym}")
        submit_limit("buy", sym, qty, entry_price)
        return
    if add_used.get(sym, False):
        log(f"ℹ️ ADD ignored {sym}: already added once")
        return
    if not in_profit(sym):
        log(f"🚫 ADD blocked {sym}: not in profit")
        return

    if submit_limit("buy", sym, qty, entry_price):
        add_used[sym] = True
        log(f"➕ ADD filled/placed {sym}")

def do_exit(sym, exit_price=None):
    if safe_qty(sym) <= 0:
        log(f"ℹ️ EXIT ignored {sym}: flat")
        return

    # Try alert target first (if supplied), else jump straight to aggressive loop
    if exit_price is not None and float(exit_price) > 0:
        tgt = round_tick(float(exit_price))
        cancel_open_orders(sym)
        log(f"🔔 EXIT try target {sym} @ {tgt}")
        submit_limit("sell", sym, safe_qty(sym), tgt)
        time.sleep(6)
        if safe_qty(sym) > 0:
            aggressive_limit_exit(sym, tgt, reason="EXIT_FALLBACK")
        else:
            _log_pnl(sym, tgt, "EXIT_TARGET", "all")
    else:
        bid, ask, last = latest_bid_ask_trade(sym)
        start = bid or last or ask
        if not start:
            log(f"⚠️ EXIT {sym}: no reference price; skipping")
            return
        aggressive_limit_exit(sym, start, reason="EXIT_NO_TARGET")

# ──────────────────────────────
# Alert handler (threaded)
# ──────────────────────────────
def handle_alert(data):
    try:
        action       = str(data.get("action","")).upper().strip()
        sym          = str(data.get("ticker","")).upper().strip()
        qty          = float(data.get("quantity", 100))
        entry_price  = data.get("entry_price", None)
        exit_price   = data.get("exit_price",  None)
        candle_low   = data.get("candle_low",  None)
        candle_close = data.get("candle_close",None)

        log(f"🚀 {action} | {sym}")

        if action == "BUY":
            if entry_price is None:
                log("⚠️ BUY missing entry_price")
                return
            do_buy(sym, qty, float(entry_price), candle_low, candle_close)

        elif action == "ADD":
            if entry_price is None:
                log("⚠️ ADD missing entry_price")
                return
            do_add(sym, qty, float(entry_price))

        elif action == "EXIT":
            do_exit(sym, float(exit_price) if exit_price else None)

        else:
            log(f"⚠️ Unknown action '{action}' — ignored")

    except Exception as e:
        log(f"❌ handle_alert: {e}\n{traceback.format_exc()}")

# ──────────────────────────────
# Routes
# ──────────────────────────────
@app.post("/tv")
def tv():
    try:
        data = request.get_json(silent=True) or {}
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify(error="Invalid secret"), 403
        threading.Thread(target=handle_alert, args=(data,), daemon=True).start()
        return jsonify(ok=True)
    except Exception as e:
        log(f"❌ /tv error: {e}\n{traceback.format_exc()}")
        return jsonify(error="server"), 500

@app.get("/ping")
def ping():
    return jsonify(ok=True, base=ALPACA_BASE_URL, time=datetime.now(NY).isoformat())

# ──────────────────────────────
# Main
# ──────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))






































