import os, json, time, threading
from datetime import datetime, time as dt_time, timedelta
from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
API_KEY   = os.environ.get("ALPACA_API_KEY")
SECRET    = os.environ.get("ALPACA_SECRET_KEY")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

if not API_KEY or not SECRET:
    print("FATAL: Missing ALPACA_API_KEY/ALPACA_SECRET_KEY in environment.")
    raise SystemExit(1)

trading_client = TradingClient(API_KEY, SECRET, paper=True)
data_client    = StockHistoricalDataClient(API_KEY, SECRET)

app = Flask(__name__)

# Single source of truth for symbol state
# STATE[sym] = {"qty": float, "avg_entry": float, "stop": float, "add_used": bool, "source": str}
STATE = {}

# Guards / constants
RANGE_GUARD = 0.11  # 11%
OPEN_START  = dt_time(9, 30)
OPEN_END    = dt_time(9, 45)

# ─────────────────────────────────────────────────────────────────────────────
# UTILS
# ─────────────────────────────────────────────────────────────────────────────
def log(msg): print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def nfloat(v):
    try:
        if v is None or v == "": return None
        return float(v)
    except (TypeError, ValueError):
        return None

def now_et_time():
    # Simple UTC→ET approx; for precision use Alpaca clock
    return (datetime.utcnow() - timedelta(hours=5)).time()

def within_open_window():
    t = now_et_time()
    return OPEN_START <= t <= OPEN_END

def latest_bid(sym):
    q = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[sym]))
    return q[sym].bid_price

def compute_stop(entry, signal_low):
    lo = nfloat(signal_low)
    if within_open_window():
        floor_3pct = entry * 0.97
        return round(min(floor_3pct, lo)) if lo else round(floor_3pct, 2)
    return round(lo, 2) if lo else None

# ─────────────────────────────────────────────────────────────────────────────
# ORDER PATHS
# ─────────────────────────────────────────────────────────────────────────────
def aggressive_close(sym, reason, ref_price=None):
    """Sell using fast limits, then force market close if needed."""
    log(f"🏃 Aggressive close {sym} | reason={reason}")
    try:
        # Try a few aggressive limits near bid
        for i in range(4):
            try:
                bid = latest_bid(sym)
                if bid and bid > 0:
                    pos = trading_client.get_open_position(sym)
                    if float(pos.qty) <= 0: break
                    order = LimitOrderRequest(
                        symbol=sym, qty=pos.qty, side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC, limit_price=round(bid - 0.01, 2)
                    )
                    trading_client.submit_order(order)
                    log(f"  • try {i+1}/4: SELL {sym} @{round(bid-0.01,2)}")
                    time.sleep(2)
                else:
                    break
            except Exception as e:
                log(f"    limit error: {e}")
                time.sleep(1)

        # Final assurance: market close
        try:
            trading_client.close_position(sym, ClosePositionRequest(percentage=100))
            log(f"  • final market close submitted for {sym}")
        except Exception as e:
            log(f"  • market close error {sym}: {e}")

    except Exception as e:
        log(f"❌ aggressive_close fatal {sym}: {e}")

def start_stop_watcher(sym):
    def loop():
        log(f"🚀 stop watcher started for {sym}")
        while True:
            try:
                if sym not in STATE: break
                stop = STATE[sym].get("stop")
                if stop is None: time.sleep(1); continue

                bid = latest_bid(sym)
                if bid is not None and bid <= float(stop):
                    log(f"🛑 {sym} bid {bid:.4f} <= stop {float(stop):.4f} → EXIT")
                    aggressive_close(sym, "STOP", ref_price=bid)
                    STATE.pop(sym, None)
                    break

                # Exit if flat
                try:
                    pos = trading_client.get_open_position(sym)
                    if float(pos.qty) <= 0:
                        STATE.pop(sym, None)
                        break
                except Exception:
                    STATE.pop(sym, None)
                    break

                time.sleep(1)
            except Exception as e:
                log(f"watcher err {sym}: {e}")
                time.sleep(2)
        log(f"🧹 stop watcher ended for {sym}")
    threading.Thread(target=loop, daemon=True).start()

def handle_buy_or_add(data):
    sym        = data["ticker"]
    action     = data["action"]  # BUY or ADD
    qty        = int(data.get("quantity", 100))
    entry_px   = nfloat(data.get("entry_price"))
    signal_low = nfloat(data.get("signal_low"))
    signal_cls = nfloat(data.get("signal_close"))
    source     = data.get("source", "UNKNOWN")

    if entry_px is None:
        log(f"🚫 {sym} {action} rejected: missing entry_price")
        return

    # Range guard for both sources
    if source in ("ITG_SCALPER", "HAMMER_ENGULFING"):
        if signal_low is None or signal_cls is None:
            log(f"ℹ️ {sym} range guard skipped (no low/close)")
        else:
            rng = (signal_cls - signal_low) / max(signal_low, 1e-9)
            log(f"🔍 {sym} low→close range {rng:.2%} (limit {RANGE_GUARD:.0%})")
            if rng > RANGE_GUARD:
                log(f"🚫 {sym} {action} blocked by 11% range guard")
                return

    if action == "BUY" and sym in STATE:
        log(f"ℹ️ {sym} BUY ignored: already long")
        return

    if action == "ADD":
        if sym not in STATE:
            log(f"ℹ️ {sym} ADD ignored: not in position")
            return
        if STATE[sym].get("add_used"):
            log(f"ℹ️ {sym} ADD ignored: already used")
            return
        try:
            bid = latest_bid(sym)
            if bid is None or bid <= STATE[sym]["avg_entry"]:
                log(f"🚫 {sym} ADD blocked: not in profit")
                return
        except Exception as e:
            log(f"bid check failed for ADD {sym}: {e}")
            return

    try:
        order = LimitOrderRequest(
            symbol=sym, qty=qty, side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC, limit_price=round(entry_px, 2)
        )
        trading_client.submit_order(order)
        log(f"📥 {action} {sym} x{qty} @ {round(entry_px,2)} [{source}]")

        stop = compute_stop(entry_px, signal_low)
        state = STATE.get(sym, {"qty": 0, "avg_entry": entry_px})
        state.update({
            "source": source,
            "stop": stop,
            "add_used": state.get("add_used", False) or (action == "ADD"),
            "avg_entry": state.get("avg_entry", entry_px)
        })
        STATE[sym] = state
        start_stop_watcher(sym)

    except Exception as e:
        log(f"❌ submit {action} error {sym}: {e}")

def handle_exit(data):
    sym       = data["ticker"]
    exit_px   = nfloat(data.get("exit_price"))

    try:
        if exit_px is not None:
            # place a quick limit first
            try:
                pos = trading_client.get_open_position(sym)
                if float(pos.qty) > 0:
                    order = LimitOrderRequest(
                        symbol=sym, qty=pos.qty, side=OrderSide.SELL,
                        time_in_force=TimeInForce.GTC, limit_price=round(exit_px, 2)
                    )
                    trading_client.submit_order(order)
                    log(f"🔔 EXIT try {sym} @ {round(exit_px,2)}")
                    time.sleep(4)
            except Exception as e:
                log(f"limit exit submit err {sym}: {e}")

        # ensure flat
        aggressive_close(sym, "EXIT_ALERT", ref_price=exit_px)
        STATE.pop(sym, None)

    except Exception as e:
        log(f"❌ EXIT error {sym}: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# BACKGROUND DISPATCH
# ─────────────────────────────────────────────────────────────────────────────
def dispatch(data):
    try:
        action = data.get("action")
        if action in ("BUY", "ADD"):
            handle_buy_or_add(data)
        elif action == "EXIT":
            handle_exit(data)
        else:
            log(f"⚠️ unknown action: {action}")
    except Exception as e:
        log(f"dispatch error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})

@app.route("/echo", methods=["POST"])
def echo():
    data = request.get_json(force=True) or {}
    return jsonify({"received": data, "valid_secret": data.get("secret") == WEBHOOK_SECRET})

@app.route("/tv", methods=["POST"])
def tv():
    data = request.get_json(force=True) or {}
    # auth first
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Invalid secret"}), 403
    # required fields
    if not data.get("action") or not data.get("ticker"):
        return jsonify({"error": "Missing action/ticker"}), 400

    log(f"📡 {data.get('action')} {data.get('ticker')} | src={data.get('source')}")
    # immediate 200 to avoid 499s
    threading.Thread(target=dispatch, args=(data,), daemon=True).start()
    return jsonify({"status": "ok"}), 200

# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Local run (Railway/Gunicorn will call app externally)
    app.run(host="0.0.0.0", port=8080)



















































