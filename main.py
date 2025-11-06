# main.py — Clean Alpaca Execution Bot (Railway 2025 Edition)
# ============================================================
# ✅ Works with alpaca-py ≥0.16
# ✅ Handles pre/post-market via marketable limit orders
# ✅ Multi-ticker + synthetic SL/TP
# ✅ Logs clear auth errors if environment variables missing
# ============================================================

import os, time, threading
from datetime import datetime, timezone, date
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# -------------------------------
# CONFIGURATION
# -------------------------------
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
SECRET_WEBHOOK = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

POLL_INTERVAL = 1.5        # seconds between quote checks
SLIP = 0.01                # offset to make limits marketable
MAX_LOSSES = 2             # per ticker per UTC day
DEFAULT_QTY = 100
USE_EXTENDED = True        # pre/post-market trading

# --- Safety check for environment variables ---
if not API_KEY or not SECRET_KEY:
    raise RuntimeError("🚨 Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in Railway variables!")

# --- Initialize Alpaca clients with keyword args ---
trading = TradingClient(api_key=API_KEY, secret_key=SECRET_KEY, paper=True)
data = StockHistoricalDataClient(api_key=API_KEY, secret_key=SECRET_KEY)
app = Flask(__name__)

# -------------------------------
# STATE
# -------------------------------
POSITIONS = {}     # active trades
LOSS_LOG = {}      # daily loss counters
LOCK = threading.Lock()

# -------------------------------
# UTILITIES
# -------------------------------
def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%SZ')}] {msg}", flush=True)

def today():
    return datetime.now(timezone.utc).date()

def get_quote(symbol):
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        q = data.get_stock_latest_quote(req)
        q = q[symbol] if isinstance(q, dict) else q
        return float(q.bid_price or 0), float(q.ask_price or 0)
    except Exception as e:
        log(f"⚠️ Quote error for {symbol}: {e}")
        return 0, 0

def marketable_limit(side, bid, ask):
    if side == OrderSide.BUY:
        return round((ask or bid) + SLIP, 2)
    else:
        return round(max(0.01, (bid or ask) - SLIP), 2)

def submit(symbol, side, qty, price):
    try:
        req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=side,
            limit_price=price,
            time_in_force=TimeInForce.DAY,
            extended_hours=USE_EXTENDED
        )
        trading.submit_order(req)
        log(f"📤 {side.name} {symbol} @{price:.2f}")
        return True
    except Exception as e:
        log(f"❌ Order error for {symbol}: {e}")
        return False

# -------------------------------
# LOSS CONTROL
# -------------------------------
def reset_loss(symbol):
    if symbol not in LOSS_LOG or LOSS_LOG[symbol]['day'] != today():
        LOSS_LOG[symbol] = {'day': today(), 'count': 0}

def add_loss(symbol):
    reset_loss(symbol)
    LOSS_LOG[symbol]['count'] += 1

def blocked(symbol):
    reset_loss(symbol)
    return LOSS_LOG[symbol]['count'] >= MAX_LOSSES

# -------------------------------
# EXECUTION
# -------------------------------
def buy(symbol, qty, entry_close, signal_low):
    with LOCK:
        if blocked(symbol):
            log(f"⛔ BUY blocked for {symbol}: {MAX_LOSSES} losses reached.")
            return
        if symbol in POSITIONS:
            log(f"⚠️ Already holding {symbol}.")
            return

        bid, ask = get_quote(symbol)
        price = marketable_limit(OrderSide.BUY, bid, ask)
        if submit(symbol, OrderSide.BUY, qty, price):
            POSITIONS[symbol] = {
                'entry': entry_close,
                'stop': signal_low,
                'qty': qty,
                'time': datetime.now(timezone.utc).isoformat()
            }
            log(f"✅ Tracking {symbol} | entry_ref={entry_close:.4f} stop={signal_low:.4f}")

def sell(symbol, reason):
    with LOCK:
        pos = POSITIONS.pop(symbol, None)
    if not pos:
        log(f"ℹ️ No position to sell for {symbol}")
        return

    bid, ask = get_quote(symbol)
    price = marketable_limit(OrderSide.SELL, bid, ask)
    if submit(symbol, OrderSide.SELL, pos['qty'], price):
        pnl = (price - pos['entry']) / max(pos['entry'], 0.0001)
        if pnl < 0:
            add_loss(symbol)
        log(f"💰 EXIT {symbol} @{price:.2f} | PnL={pnl*100:.2f}% | reason={reason}")

# -------------------------------
# MONITOR LOOP
# -------------------------------
def monitor_positions():
    while True:
        with LOCK:
            symbols = list(POSITIONS.keys())
        for sym in symbols:
            bid, _ = get_quote(sym)
            with LOCK:
                pos = POSITIONS.get(sym)
            if not pos:
                continue
            if bid and bid <= pos['stop']:
                log(f"💀 STOP HIT {sym} @ {bid:.2f}")
                sell(sym, "STOP_HIT")
        time.sleep(POLL_INTERVAL)

# -------------------------------
# WEBHOOK
# -------------------------------
@app.route("/tv", methods=["POST"])
def tv():
    data_in = request.get_json(force=True)
    if data_in.get("secret") != SECRET_WEBHOOK:
        return jsonify({"error": "invalid secret"}), 403

    symbol = str(data_in.get("ticker", "")).upper()
    action = str(data_in.get("action", "")).upper()
    qty = int(data_in.get("quantity", DEFAULT_QTY))

    if action == "BUY":
        entry = float(data_in["close"])
        low = float(data_in["signal_low"])
        threading.Thread(target=buy, args=(symbol, qty, entry, low), daemon=True).start()
        log(f"📡 BUY {symbol} | close={entry} low={low}")
        return jsonify({"ok": True}), 200

    elif action == "SELL":
        threading.Thread(target=sell, args=(symbol, "TAKE_PROFIT"), daemon=True).start()
        log(f"📡 SELL {symbol}")
        return jsonify({"ok": True}), 200

    else:
        return jsonify({"error": "unknown action"}), 400

# -------------------------------
# RUN
# -------------------------------
if __name__ == "__main__":
    log("🚀 Starting Alpaca Bot on Railway (v2025)")
    threading.Thread(target=monitor_positions, daemon=True).start()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))




















































