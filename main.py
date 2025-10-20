# © Chris / Athena — chrisbot1501
# Flask webhook for TradingView → Alpaca with EMA20-proximity entries
# and synthetic trailing exits (pre-market compatible)

from flask import Flask, request, jsonify
import os, json, time, threading, math
from datetime import datetime, timezone, timedelta
from alpaca_trade_api.rest import REST, TimeFrame, APIError

app = Flask(__name__)

# ──────────────────────────────
# ENV / CONFIG
# ──────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

# Entry & exit rules
EMA_PROX_PCT        = 3.0       # must be within ±3% of EMA20 to enter
ENTRY_LIMIT_BUFFER  = 0.003     # +0.3% on entry limit to improve fill chance
EXIT_LIMIT_BUFFER   = 0.003     # -0.3% on pre/post "synthetic market" sells
HARD_STOP_PCT       = 5.0       # hard stop (from avg entry)
TRAIL_TP_DD_PCT     = 10.0      # trailing TP: sell all on 10% drawdown from peak
POLL_INTERVAL_S     = 3         # trailing loop poll
CANCEL_AFTER_SEC    = 30        # cancel stale open BUY limit after this
DEFAULT_TF          = "5Min"    # fallback timeframe if alert omits bar_tf

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')

# Runtime state for trailing monitors
_monitors = {}
_mon_lock = threading.Lock()

# ──────────────────────────────
# UTILITIES
# ──────────────────────────────
def log(msg: str):
    now = datetime.now(timezone.utc).astimezone()
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S %Z')}] {msg}")

def is_rth() -> bool:
    try:
        clock = api.get_clock()
        return bool(clock.is_open)
    except Exception as e:
        log(f"⚠️ get_clock failed; assuming extended hours: {e}")
        return False

def get_live_price(symbol: str) -> float | None:
    try:
        t = api.get_latest_trade(symbol)
        return float(t.price)
    except Exception as e:
        log(f"❌ live price error {symbol}: {e}")
        return None

def tf_to_alpaca(tf_str: str) -> TimeFrame:
    tf_str = (tf_str or DEFAULT_TF).strip()
    table = {
        "1Min": TimeFrame.Minute,
        "2Min": TimeFrame(2, "Min"),
        "3Min": TimeFrame(3, "Min"),
        "4Min": TimeFrame(4, "Min"),
        "5Min": TimeFrame(5, "Min"),
        "15Min": TimeFrame(15, "Min")
    }
    return table.get(tf_str, TimeFrame.Minute)

def ema(values, length: int) -> float | None:
    if not values or len(values) < length:
        return None
    alpha = 2 / (length + 1)
    e = values[0]
    for v in values[1:]:
        e = alpha * v + (1 - alpha) * e
    return float(e)

def get_ema20_from_alpaca(symbol: str, tf_str: str) -> float | None:
    """Pull recent bars for the requested timeframe and compute EMA-20 on closes."""
    try:
        tf = tf_to_alpaca(tf_str)
        # Get enough bars to compute a stable EMA20
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=3)  # plenty for intraday tf
        bars = list(api.get_bars(symbol, tf, start, end))
        closes = [float(b.c) for b in bars][-120:]  # last 120 for stability
        e = ema(closes, 20)
        return e
    except Exception as e:
        log(f"❌ get_ema20 error {symbol}/{tf_str}: {e}")
        return None

def pct_diff(a: float, b: float) -> float:
    if b == 0:
        return 999
    return abs(a - b) / b * 100.0

def cancel_open_orders_for(symbol: str):
    try:
        orders = api.list_orders(status="open")
        for o in orders:
            if o.symbol == symbol:
                try:
                    api.cancel_order(o.id)
                    log(f"❎ Cancelled open order {o.id} ({symbol})")
                except Exception as ce:
                    log(f"❌ Cancel failed {o.id} ({symbol}): {ce}")
    except Exception as e:
        log(f"❌ list_orders failed: {e}")

# ──────────────────────────────
# ORDER HELPERS
# ──────────────────────────────
def submit_buy_limit_day(symbol: str, qty: float, ref_price: float):
    """Place a DAY limit buy around the EMA20 reference. Works in pre/post & RTH."""
    limit_price = round(ref_price * (1 + ENTRY_LIMIT_BUFFER), 4)
    cancel_open_orders_for(symbol)
    ord = api.submit_order(
        symbol=symbol,
        qty=qty,
        side="buy",
        type="limit",
        time_in_force="day",
        limit_price=str(limit_price),
        extended_hours=not is_rth()
    )
    log(f"🟢 BUY limit placed {symbol} qty={qty} @ {limit_price} (ref EMA20={ref_price:.4f}) id={getattr(ord, 'id', ord)}")
    time.sleep(CANCEL_AFTER_SEC)
    cancel_open_orders_for(symbol)

def submit_exit_all_fast(symbol: str, qty: float):
    live = get_live_price(symbol)
    if live is None:
        raise RuntimeError("No live price for EXIT")
    if is_rth():
        ord = api.submit_order(
            symbol=symbol, qty=qty, side="sell",
            type="market", time_in_force="day"
        )
        log(f"🛑 RTH MARKET SELL {symbol} qty={qty} id={getattr(ord, 'id', ord)}")
    else:
        limit_price = round(live * (1 - EXIT_LIMIT_BUFFER), 4)
        cancel_open_orders_for(symbol)
        ord = api.submit_order(
            symbol=symbol, qty=qty, side="sell",
            type="limit", time_in_force="day",
            limit_price=str(limit_price), extended_hours=True
        )
        log(f"🕓 EXT LIMIT SELL {symbol} qty={qty} @ {limit_price} id={getattr(ord, 'id', ord)}")

# ──────────────────────────────
# TRAILING WATCHER
# ──────────────────────────────
def start_trailer(symbol: str):
    with _mon_lock:
        if symbol in _monitors and _monitors[symbol].get("running"):
            log(f"⏭️ trailer already running for {symbol}")
            return
        live = get_live_price(symbol) or 0.0
        _monitors[symbol] = {"running": True, "high": live}
    threading.Thread(target=_trailer_loop, args=(symbol,), daemon=True).start()

def _trailer_loop(symbol: str):
    log(f"👀 trailer start {symbol} | stop={HARD_STOP_PCT}% | trailDD={TRAIL_TP_DD_PCT}%")
    try:
        while True:
            with _mon_lock:
                if symbol not in _monitors or not _monitors[symbol]["running"]:
                    break
            # confirm we still hold
            try:
                pos = api.get_position(symbol)
                qty = abs(float(pos.qty))
                avg = float(pos.avg_entry_price)
                if qty <= 0:
                    log(f"ℹ️ no qty for {symbol}, stopping trailer")
                    break
            except APIError:
                log(f"ℹ️ position not found for {symbol}, stopping trailer")
                break
            except Exception as e:
                log(f"⚠️ get_position error {symbol}: {e}")
                time.sleep(POLL_INTERVAL_S); continue

            live = get_live_price(symbol)
            if live is None:
                time.sleep(POLL_INTERVAL_S); continue

            # update peak
            with _mon_lock:
                if live > _monitors[symbol]["high"]:
                    _monitors[symbol]["high"] = live
                peak = _monitors[symbol]["high"]

            # hard stop
            if HARD_STOP_PCT and live <= avg * (1 - HARD_STOP_PCT/100):
                log(f"🚨 HARD STOP {symbol} live={live:.4f} avg={avg:.4f}")
                submit_exit_all_fast(symbol, qty)
                break

            # trailing drawdown exit
            if peak > 0 and live <= peak * (1 - TRAIL_TP_DD_PCT/100):
                dd = 100*(1 - live/peak)
                log(f"🏁 TRAIL EXIT {symbol} peak={peak:.4f} live={live:.4f} dd={dd:.2f}%")
                submit_exit_all_fast(symbol, qty)
                break

            time.sleep(POLL_INTERVAL_S)
    finally:
        with _mon_lock:
            if symbol in _monitors:
                _monitors[symbol]["running"] = False
        log(f"✅ trailer end {symbol}")

def wait_for_fill_then_trail(symbol: str, tries: int = 20, sleep_s: int = 3):
    for _ in range(tries):
        try:
            pos = api.get_position(symbol)
            if float(pos.qty) > 0:
                log(f"✅ fill detected {symbol} qty={pos.qty} avg={pos.avg_entry_price}")
                start_trailer(symbol)
                return
        except APIError:
            pass
        except Exception as e:
            log(f"⚠️ get_position while waiting {symbol}: {e}")
        time.sleep(sleep_s)
    log(f"⚠️ no fill detected for {symbol}; trailer not started")

# ──────────────────────────────
# CORE HANDLERS
# ──────────────────────────────
def handle_buy(symbol: str, qty: float, alert_ema20: float | None, bar_tf: str | None):
    # 1) Determine EMA20 reference:
    ema20 = None
    if alert_ema20 is not None:
        try:
            ema20 = float(alert_ema20)
        except:
            ema20 = None
    if ema20 is None:
        ema20 = get_ema20_from_alpaca(symbol, bar_tf or DEFAULT_TF)

    if ema20 is None:
        log(f"❌ cannot obtain EMA20 for {symbol}; skipping BUY")
        return

    live = get_live_price(symbol)
    if live is None:
        log(f"❌ cannot obtain live price for {symbol}; skipping BUY")
        return

    # 2) Proximity check (within ±3% of EMA20)
    diff = pct_diff(live, ema20)
    if diff > EMA_PROX_PCT:
        log(f"⏸️ skipped BUY {symbol}: live={live:.4f} ema20={ema20:.4f} | diff={diff:.2f}% > {EMA_PROX_PCT}%")
        return

    # 3) Place DAY limit BUY around EMA20 (works pre/post & RTH)
    submit_buy_limit_day(symbol, qty, ema20)

    # 4) Start trailing manager once the position fills
    threading.Thread(target=wait_for_fill_then_trail, args=(symbol,), daemon=True).start()

def handle_exit(symbol: str):
    try:
        pos = api.get_position(symbol)
        q = abs(float(pos.qty))
        if q > 0:
            submit_exit_all_fast(symbol, q)
        else:
            log(f"ℹ️ EXIT requested but no qty for {symbol}")
    except APIError:
        log(f"ℹ️ EXIT requested but no open position for {symbol}")
    finally:
        with _mon_lock:
            if symbol in _monitors:
                _monitors[symbol]["running"] = False

# ──────────────────────────────
# WEBHOOK
# ──────────────────────────────
@app.route("/tv", methods=["POST"])
def tv():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        log("⚠️ invalid secret")
        return jsonify(error="Invalid secret"), 403

    log(f"🚀 Alert: {json.dumps(data)}")

    event   = str(data.get("event", "")).upper()
    symbol  = str(data.get("ticker", "")).upper()
    qty     = float(data.get("qty", 1) or 1)
    ema20_a = data.get("ema20")            # optional, from TV
    bar_tf  = data.get("bar_tf", DEFAULT_TF)

    try:
        if event == "BUY":
            handle_buy(symbol, qty, ema20_a, bar_tf)
        elif event in ("EXIT", "SELL", "TP", "STOP"):
            handle_exit(symbol)
        else:
            log(f"⚠️ unknown event: {event}")
    except Exception as e:
        log(f"❌ handler error: {e}")

    return jsonify(status="ok"), 200

# ──────────────────────────────
# RUN
# ──────────────────────────────
if __name__ == "__main__":
    log("🚀 ChrisBot1501 running (EMA20 proximity entries + synthetic trailing exits)")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
















