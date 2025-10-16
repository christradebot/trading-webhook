# main.py
# Flask webhook → Alpaca trading bot (long-only)
# - BUY: queue IOC limit buy at min(VWAP, EMA9); retry until N bars (5m bars)
# - SELL: exit long (RTH=market, pre/post=limit IOC)
# - Hot-stop: auto exit at -20% unrealized (RTH=market, pre/post=limit IOC)
# - Daily 04:00 ET reset: flatten all + reset per-ticker counters
# - Emergency kill: POST /kill {"secret":"..."}
# - Safety: duplicate guard, max 5 trades/ticker/day, IOC (no lingering partials)
#
# Requires: flask, alpaca_trade_api, pytz

import os, json, time, threading, hashlib
from datetime import datetime, timedelta
import pytz

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST, TimeFrame

# ───────────── Config / ENV ─────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "mysecret")

# Safety knobs
MAX_TRADES_PER_TICKER_PER_DAY = 5         # cap entries per symbol per ET day
ENTRY_BUFFER_PCT   = 0.0015                # 0.15% below target for buy limit
HOT_STOP_PCT       = 20.0                  # exit if unrealized loss >= 20%
HOT_STOP_CHECK_S   = 5                     # hot-stop check frequency (s)
PREPOST_LIMIT_SLIP_PCT = 0.005             # -0.5% buffer for pre/post exits
DUP_WINDOW_SEC     = 5                     # ignore duplicate alerts within 5s
DEFAULT_BAR_SEC    = 300                   # 5-minute bars
DEFAULT_BARS_WIN   = 5                     # # of bars for entry window
ROUND_PRICE_PLACES = 4                     # price rounding for limit orders

NY = pytz.timezone("America/New_York")

app = Flask(__name__)
api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')

# ───────────── State ─────────────
trade_counter = {}       # { "YYYY-MM-DD": { "TICKER": count } }
pending_entries = {}     # { "TICKER": {"remaining":int,"deadline":epoch,"target":float,"extended":bool,"last_alert_price":float} }
recent_alerts = {}       # { sha256: epoch_ts }

# ───────────── Helpers ─────────────
def now_et():
    return datetime.now(NY)

def today_key_et():
    return now_et().strftime("%Y-%m-%d")

def in_regular_hours_et(dt=None):
    dt = dt or now_et()
    t = dt.time()
    return t >= datetime.strptime("09:30","%H:%M").time() and t < datetime.strptime("16:00","%H:%M").time()

def is_pre_or_post(dt=None):
    dt = dt or now_et()
    t = dt.time()
    pre  = t >= datetime.strptime("04:00","%H:%M").time() and t < datetime.strptime("09:30","%H:%M").time()
    post = t >= datetime.strptime("16:00","%H:%M").time() and t < datetime.strptime("20:00","%H:%M").time()
    return pre or post

def bump_counter(ticker, n=1):
    k = today_key_et()
    trade_counter.setdefault(k, {})
    trade_counter[k][ticker] = trade_counter[k].get(ticker, 0) + n
    return trade_counter[k][ticker]

def get_counter(ticker):
    k = today_key_et()
    return trade_counter.get(k, {}).get(ticker, 0)

def reset_counters_for_new_day():
    trade_counter.clear()

def clean_recent_alerts():
    now_ts = time.time()
    for h, ts in list(recent_alerts.items()):
        if now_ts - ts > 30:
            recent_alerts.pop(h, None)

def hash_alert(payload: dict):
    s = json.dumps(payload, sort_keys=True, separators=(",",":"))
    return hashlib.sha256(s.encode()).hexdigest()

def round_price(x):
    return round(float(x), ROUND_PRICE_PLACES)

def latest_price(symbol, fallback=None):
    # Try latest trade; if not available, use fallback (from position or alert)
    try:
        lt = api.get_last_trade(symbol)
        return float(lt.price)
    except Exception:
        return float(fallback) if fallback is not None else None

def submit_limit_ioc_buy(symbol, qty, limit_price, extended=True):
    return api.submit_order(
        symbol=symbol,
        qty=qty,
        side="buy",
        type="limit",
        time_in_force="ioc",
        limit_price=limit_price,
        extended_hours=extended
    )

def submit_market_sell(symbol, qty):
    return api.submit_order(
        symbol=symbol,
        qty=qty,
        side="sell",
        type="market",
        time_in_force="day"
    )

def submit_limit_ioc_sell(symbol, qty, limit_price, extended=True):
    return api.submit_order(
        symbol=symbol,
        qty=qty,
        side="sell",
        type="limit",
        time_in_force="ioc",
        limit_price=limit_price,
        extended_hours=extended
    )

def flatten_all_positions():
    try:
        api.cancel_all_orders()
    except Exception as e:
        print(f"[FLATTEN] cancel_all_orders error: {e}")

    try:
        positions = api.list_positions()
        for p in positions:
            q = abs(int(float(p.qty)))
            if q <= 0:
                continue
            if p.side.lower() == "long":
                if in_regular_hours_et():
                    submit_market_sell(p.symbol, q)
                    print(f"[FLATTEN] MARKET sell {p.symbol} x{q}")
                else:
                    last = latest_price(p.symbol, p.current_price)
                    if last:
                        lim = round_price(last * (1 - PREPOST_LIMIT_SLIP_PCT))
                        submit_limit_ioc_sell(p.symbol, q, lim, extended=True)
                        print(f"[FLATTEN] LIMIT-IOC sell {p.symbol} x{q} @ {lim}")
            else:
                # If short ever appears, just buy-to-cover market
                api.submit_order(symbol=p.symbol, qty=q, side="buy", type="market", time_in_force="day")
                print(f"[FLATTEN] MARKET buy-to-cover {p.symbol} x{q}")
    except Exception as e:
        print(f"[FLATTEN] positions error: {e}")

# ───────────── Workers ─────────────
def pending_entry_worker():
    """Try IOC limit buys at/under target until deadline expires."""
    while True:
        try:
            now_ts = time.time()
            for sym, st in list(pending_entries.items()):
                if now_ts > st["deadline"]:
                    print(f"[ENTRY] window expired for {sym}, remaining {st['remaining']}; dropping intent.")
                    pending_entries.pop(sym, None)
                    continue

                if st["remaining"] <= 0:
                    pending_entries.pop(sym, None)
                    continue

                last = latest_price(sym, st.get("last_alert_price"))
                if last is None:
                    continue

                # For a BUY, we want price <= target to avoid chasing
                if last <= st["target"]:
                    qty = int(st["remaining"])
                    lim = round_price(st["target"] * (1 - ENTRY_BUFFER_PCT))
                    try:
                        o = submit_limit_ioc_buy(sym, qty, lim, extended=st["extended"])
                        # IOC either fills now or cancels unfilled remainder automatically
                        # Re-fetch the order to see fill details (best effort)
                        try:
                            o_ref = api.get_order(o.id)
                            filled = int(float(getattr(o_ref, "filled_qty", "0")))
                        except Exception:
                            filled = 0
                        remaining = max(0, qty - filled)
                        st["remaining"] = remaining
                        print(f"[ENTRY] {sym} IOC try {qty} @ {lim}; filled {filled}; remaining {remaining}")
                        if remaining <= 0:
                            pending_entries.pop(sym, None)
                            bump_counter(sym, 1)
                    except Exception as e:
                        print(f"[ENTRY] submit IOC buy error {sym}: {e}")
            time.sleep(1.0)
        except Exception as e:
            print(f"[ENTRY] worker error: {e}")
            time.sleep(2.0)

def hot_stop_worker():
    """Exit any long losing >= HOT_STOP_PCT."""
    while True:
        try:
            positions = api.list_positions()
            for p in positions:
                if p.side.lower() != "long":
                    continue
                entry = float(p.avg_entry_price)
                last  = latest_price(p.symbol, p.current_price)
                if not last or entry <= 0:
                    continue
                dd = (entry - last) / entry * 100.0
                if dd >= HOT_STOP_PCT:
                    qty = abs(int(float(p.qty)))
                    print(f"[HOTSTOP] {p.symbol} -{dd:.1f}% → emergency exit ({'RTH' if in_regular_hours_et() else 'EXT'})")
                    try:
                        if in_regular_hours_et():
                            submit_market_sell(p.symbol, qty)
                            print(f"[HOTSTOP] MARKET sell {p.symbol} x{qty}")
                        else:
                            lim = round_price(last * (1 - PREPOST_LIMIT_SLIP_PCT))
                            submit_limit_ioc_sell(p.symbol, qty, lim, extended=True)
                            print(f"[HOTSTOP] LIMIT-IOC sell {p.symbol} x{qty} @ {lim}")
                    except Exception as e:
                        print(f"[HOTSTOP] order error {p.symbol}: {e}")
                    time.sleep(60)  # cooldown to avoid re-fire spam
        except Exception as e:
            print(f"[HOTSTOP] loop error: {e}")
        time.sleep(HOT_STOP_CHECK_S)

def seconds_to_next_4am_et():
    now = now_et()
    tgt = now.replace(hour=4, minute=0, second=0, microsecond=0)
    if now >= tgt:
        tgt += timedelta(days=1)
    return max(1, int((tgt - now).total_seconds()))

def daily_reset_worker():
    """Flatten and reset trade counters at 04:00 ET daily."""
    while True:
        try:
            wait_s = seconds_to_next_4am_et()
            print(f"[RESET] sleeping {wait_s}s until 04:00 ET")
            time.sleep(wait_s)
            print("[RESET] 04:00 ET → flatten & reset counters")
            flatten_all_positions()
            reset_counters_for_new_day()
            time.sleep(65)  # avoid double within same minute
        except Exception as e:
            print(f"[RESET] worker error: {e}")
            time.sleep(30)

threading.Thread(target=pending_entry_worker, daemon=True).start()
threading.Thread(target=hot_stop_worker, daemon=True).start()
threading.Thread(target=daily_reset_worker, daemon=True).start()

# ───────────── Routes ─────────────
@app.get("/ping")
def ping():
    # Debug helper: shows the currently loaded secret length (not value)
    return jsonify(ok=True,
                   service="tv-alpaca",
                   et_time=now_et().isoformat(),
                   secret_len=len(WEBHOOK_SECRET or ""),
                   vars_loaded=bool(ALPACA_KEY_ID and ALPACA_SECRET_KEY)), 200

@app.post("/kill")
def kill():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403
    print("[EMERGENCY] Kill switch triggered")
    flatten_all_positions()
    return jsonify(status="ok", message="All positions flattened"), 200

@app.post("/tv")
def webhook():
    clean_recent_alerts()

    # ── Deep DEBUG: raw body + robust JSON parse ──
    raw_body = request.data.decode(errors="replace")
    print("🟡 RAW POST BODY:", raw_body)
    data = None
    try:
        data = request.get_json(force=True)
        print("🟢 PARSED JSON:", json.dumps(data, indent=2))
    except Exception as e:
        print("🔴 JSON PARSE ERROR:", e)

    if not data:
        return jsonify(error="No data"), 400
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403

    # Duplicate guard (ignore exact repeats for a few seconds)
    sig_for_hash = {k: data[k] for k in sorted(data) if k not in ("price",)}  # ignore price noise
    h = hash_alert(sig_for_hash)
    now_ts = time.time()
    if h in recent_alerts and now_ts - recent_alerts[h] < DUP_WINDOW_SEC:
        print("[GUARD] duplicate alert, ignored")
        return jsonify(status="ok", dedup=True), 200
    recent_alerts[h] = now_ts

    # Fields
    event    = str(data.get("event","")).lower()
    symbol   = str(data.get("ticker","")).upper()
    qty      = int(float(data.get("qty", 0)))
    price_in = float(data.get("price", 0)) if data.get("price") is not None else 0.0
    extended = bool(data.get("extended_hours", True))
    vwap     = data.get("vwap", None)
    ema9     = data.get("ema9", data.get("ema_9", None))
    bar_sec  = int(data.get("bar_seconds", DEFAULT_BAR_SEC))  # 5m=300
    bars_win = int(data.get("bars_window", DEFAULT_BARS_WIN)) # default 5 bars

    print(f"[ALERT] {event.upper()} {symbol} qty={qty} ext={extended} vwap={vwap} ema9={ema9} bar={bar_sec}s win={bars_win}")

    if not symbol or qty <= 0:
        return jsonify(error="Invalid ticker/qty"), 400

    try:
        if event == "buy":
            # Max trades per ticker/day
            if get_counter(symbol) >= MAX_TRADES_PER_TICKER_PER_DAY:
                print(f"[GUARD] max trades reached for {symbol}")
                return jsonify(status="skipped", reason="max_trades_reached"), 200

            # Choose target = min(vwap, ema9); fallback to price_in if missing
            targets = []
            for val in (vwap, ema9):
                try:
                    if val is not None:
                        targets.append(float(val))
                except Exception:
                    pass
            if not targets and price_in > 0:
                targets.append(float(price_in))
            if not targets:
                return jsonify(error="No price targets provided"), 400

            target = min(targets)
            # Entry window ends after N bars
            deadline = time.time() + max(1, bars_win) * max(5, bar_sec)

            # Merge/queue intent
            cur = pending_entries.get(symbol)
            if cur:
                cur["remaining"] = max(cur["remaining"], qty)
                cur["deadline"]  = max(cur["deadline"], deadline)
                cur["target"]    = min(cur["target"], target)  # tighter is better
                cur["extended"]  = extended
                cur["last_alert_price"] = price_in
            else:
                pending_entries[symbol] = {
                    "remaining": qty,
                    "deadline":  deadline,
                    "target":    float(target),
                    "extended":  extended,
                    "last_alert_price": price_in
                }
            print(f"[ENTRY] queued {symbol}: qty={qty} target={target} until={datetime.fromtimestamp(deadline)}")
            return jsonify(status="ok", queued=True), 200

        elif event == "sell":
            # Exit long-only
            pos_map = {p.symbol: p for p in api.list_positions()}
            if symbol not in pos_map:
                print(f"[EXIT] No open position for {symbol}; skipping")
                return jsonify(status="ok", no_position=True), 200

            q = abs(int(float(pos_map[symbol].qty)))
            last = latest_price(symbol, price_in)
            if in_regular_hours_et():
                submit_market_sell(symbol, q)
                print(f"[EXIT] MARKET sell {symbol} x{q}")
            else:
                lim = round_price(last * (1 - PREPOST_LIMIT_SLIP_PCT)) if last else round_price(price_in * (1 - PREPOST_LIMIT_SLIP_PCT))
                submit_limit_ioc_sell(symbol, q, lim, extended=True)
                print(f"[EXIT] LIMIT-IOC sell {symbol} x{q} @ {lim}")

            # Cancel any pending entry intent for this symbol
            pending_entries.pop(symbol, None)
            return jsonify(status="ok"), 200

        else:
            print(f"[WARN] Unknown event '{event}'")
            return jsonify(status="ignored", reason="unknown_event"), 200

    except Exception as e:
        print(f"[ERROR] webhook: {e}")
        return jsonify(error=str(e)), 500

# ───────────── Entrypoint ─────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)














