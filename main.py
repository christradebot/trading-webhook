# === main.py ===
# © Chris / Athena 2025
# HMA-only execution bot: limit-only entries/exits around HMA with ±2% buffer

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST, TimeFrame, APIError
import os, json, time, math
from statistics import fmean

app = Flask(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# ENV / API
# ──────────────────────────────────────────────────────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")

# ──────────────────────────────────────────────────────────────────────────────
# PARAMETERS
# ──────────────────────────────────────────────────────────────────────────────
HMA_LEN              = 14
ENTRY_BUFFER_PCT     = float(os.environ.get("ENTRY_BUFFER_PCT", "0.02"))  # 2%
EXIT_BUFFER_PCT      = float(os.environ.get("EXIT_BUFFER_PCT",  "0.02"))  # 2%
CHASE_REPRICES       = int(os.environ.get("CHASE_REPRICES", "8"))         # short chase to avoid worker timeouts
CHASE_SLEEP_SEC      = float(os.environ.get("CHASE_SLEEP_SEC", "1.5"))
AGG_EXIT_EXTRA_PCT   = float(os.environ.get("AGG_EXIT_EXTRA_PCT", "0.01"))# extra 1% under bid for aggressive exit
LOG_PREFIX           = "[HMA]"

# ──────────────────────────────────────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────────────────────────────────────
def log(msg): print(f"{LOG_PREFIX} {time.strftime('%H:%M:%S')}  {msg}", flush=True)

def round_price(p: float) -> float:
    if p < 1:   return round(p, 4)
    if p < 10:  return round(p, 3)
    return round(p, 2)

def get_nbbo(symbol):
    try:
        q = api.get_latest_quote(symbol)
        bid = float(q.bid_price) if q and q.bid_price else None
        ask = float(q.ask_price) if q and q.ask_price else None
        return bid, ask
    except Exception as e:
        log(f"NBBO error {symbol}: {e}")
        return None, None

def get_last(symbol):
    try:
        t = api.get_latest_trade(symbol)
        return float(t.price) if t and t.price else None
    except Exception as e:
        log(f"Last trade error {symbol}: {e}")
        return None

# HMA calculation: HMA(n) = WMA( 2*WMA(price, n/2) - WMA(price, n), sqrt(n) )
def wma(values, length):
    if length <= 0 or len(values) < length: return None
    weights = list(range(1, length+1))
    window  = values[-length:]
    return sum(v*w for v, w in zip(window, weights)) / sum(weights)

def hull_ma(prices, n):
    if len(prices) < n: return None
    n2  = int(max(1, n/2))
    nsq = int(max(1, round(math.sqrt(n))))
    wma_n   = wma(prices, n)
    wma_n2  = wma(prices, n2)
    if wma_n is None or wma_n2 is None: return None
    series  = [2*wma_n2 - wma_n]
    # For simplicity we just use the latest value to compute final WMA:
    return wma(prices + [series[-1]], nsq) or series[-1]

def get_hma_live(symbol, bars=120):
    # minute bars; increase if you want more stability
    try:
        bars_list = api.get_bars(symbol, TimeFrame.Minute, limit=bars)
        closes = [float(b.c) for b in bars_list] if bars_list else []
        h = hull_ma(closes, HMA_LEN) if closes else None
        return h
    except Exception as e:
        log(f"HMA fetch error {symbol}: {e}")
        return None

def cancel_open_orders(symbol):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == symbol:
                api.cancel_order(o.id)
                log(f"🧹 Cancelled open order: {symbol}")
    except Exception as e:
        log(f"Cancel error {symbol}: {e}")

def get_position_qty(symbol) -> float:
    try:
        pos = api.get_position(symbol)
        return float(pos.qty)
    except APIError:
        return 0.0
    except Exception as e:
        log(f"Position error {symbol}: {e}")
        return 0.0

# ──────────────────────────────────────────────────────────────────────────────
# CORE: LIMIT-ONLY ENTRIES & EXITS AROUND HMA
# ──────────────────────────────────────────────────────────────────────────────
def place_buy_near_hma(symbol, qty, hma_hint=None):
    """
    LIMIT buy at ~HMA*(1+ENTRY_BUFFER_PCT).
    Short, safe chase (few reprices) to avoid worker timeouts.
    """
    cancel_open_orders(symbol)
    for i in range(CHASE_REPRICES):
        hma_live = get_hma_live(symbol) or hma_hint
        bid, ask = get_nbbo(symbol)
        last     = get_last(symbol)
        if not hma_live or not (bid or ask or last):
            time.sleep(CHASE_SLEEP_SEC)
            continue

        # Target buy limit slightly above HMA to favor a fill when price revisits HMA
        limit = hma_live * (1.0 + ENTRY_BUFFER_PCT)
        # To avoid overpaying massively if ask is much lower:
        if ask: limit = max(limit, ask)  # ensures immediate execution if HMA <= ask
        limit = round_price(limit)

        try:
            api.submit_order(
                symbol=symbol, qty=qty, side="buy",
                type="limit", limit_price=str(limit),
                time_in_force="day", extended_hours=True
            )
            log(f"🟢 BUY {symbol} LMT @{limit}  (HMA={round_price(hma_live)})")
        except Exception as e:
            log(f"BUY submit error {symbol}: {e}")
            break

        # Check fill quickly; if not, cancel & reprice
        time.sleep(CHASE_SLEEP_SEC)
        if get_position_qty(symbol) > 0:
            log(f"✅ Filled BUY {symbol}")
            return True
        cancel_open_orders(symbol)

    log(f"⚠️ BUY chase ended {symbol} (no fill).")
    return False

def place_sell_near_hma(symbol, qty_hint=None, hma_hint=None, aggressive=False):
    """
    LIMIT sell at ~HMA*(1-EXIT_BUFFER_PCT).
    If aggressive=True, push under bid a bit and reprice quickly to force an exit.
    """
    qty = qty_hint or get_position_qty(symbol)
    if qty <= 0:
        log(f"ℹ️ No position to close for {symbol}")
        return True

    cancel_open_orders(symbol)
    for i in range(CHASE_REPRICES if aggressive else max(3, CHASE_REPRICES//2)):
        hma_live = get_hma_live(symbol) or hma_hint
        bid, ask = get_nbbo(symbol)
        last     = get_last(symbol)
        if not (bid or ask or last):
            time.sleep(CHASE_SLEEP_SEC/2)
            continue

        if hma_live:
            limit = hma_live * (1.0 - EXIT_BUFFER_PCT)
        else:
            # fallback to bid for safety
            limit = (bid or last or 0) * (1.0 - (EXIT_BUFFER_PCT/2))

        if aggressive and bid:
            # push below bid a bit to speed the fill
            limit = min(limit, bid * (1.0 - AGG_EXIT_EXTRA_PCT))

        limit = round_price(limit)

        try:
            api.submit_order(
                symbol=symbol, qty=qty, side="sell",
                type="limit", limit_price=str(limit),
                time_in_force="day", extended_hours=True
            )
            log(f"🛑 SELL {symbol} LMT @{limit}  (HMA={round_price(hma_live) if hma_live else 'n/a'})")
        except Exception as e:
            log(f"SELL submit error {symbol}: {e}")
            break

        time.sleep(CHASE_SLEEP_SEC)
        if get_position_qty(symbol) <= 0:
            log(f"✅ Position exited {symbol}")
            return True
        cancel_open_orders(symbol)

    log(f"⚠️ SELL chase ended {symbol} (may still be holding).")
    return False

# ──────────────────────────────────────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────────────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, mode="HMA-only", entry_buffer=ENTRY_BUFFER_PCT, exit_buffer=EXIT_BUFFER_PCT)

@app.post("/tv")
def tv():
    data = request.get_json(silent=True) or {}
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify(error="Invalid secret"), 403

    log(f"🚀 TradingView Alert:\n{json.dumps(data, indent=2)}")

    action  = (data.get("action") or data.get("event") or "").upper()
    symbol  = (data.get("ticker") or "").upper()
    qty     = float(data.get("quantity", 100))
    # optional HMA from alert (preferred if you want exact chart TF)
    try:
        hma_hint = float(data.get("hma", 0.0))
    except (TypeError, ValueError):
        hma_hint = 0.0

    if not symbol or not action:
        return jsonify(ok=False, reason="missing symbol/action"), 400

    try:
        if action == "BUY":
            # avoid duplicate buys
            if get_position_qty(symbol) > 0:
                log(f"🔁 Already long {symbol}, skipping BUY.")
                return jsonify(ok=True, skipped=True)

            filled = place_buy_near_hma(symbol, qty, hma_hint=hma_hint if hma_hint > 0 else None)
            return jsonify(ok=filled)

        elif action in ("EXIT", "SELL", "TP"):
            # standard exit near HMA
            exited = place_sell_near_hma(symbol, qty_hint=None, hma_hint=hma_hint if hma_hint > 0 else None, aggressive=False)
            return jsonify(ok=exited)

        elif action in ("PANIC", "AGGRESSIVE_EXIT"):
            # aggressive emergency exit: deeper limit under bid, quick reprice loop
            exited = place_sell_near_hma(symbol, qty_hint=None, hma_hint=None, aggressive=True)
            return jsonify(ok=exited)

        else:
            log(f"⚠️ Unknown action: {action}")
            return jsonify(ok=False, reason="unknown action")

    except Exception as e:
        log(f"❌ Handler error {symbol}: {e}")
        return jsonify(error=str(e)), 500

# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)




















