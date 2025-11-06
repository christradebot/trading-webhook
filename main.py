# main.py — Yellow/Purple Candle Bot (Limit-only, Background Watcher)
# Author: Chris + Athena (2025)

import os
import json
import time
import threading
from typing import Dict, Any

from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ─────────────────────────────────────────────
# Flask
# ─────────────────────────────────────────────
app = Flask(__name__)

# ─────────────────────────────────────────────
# Env
# ─────────────────────────────────────────────
API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

if not API_KEY or not SECRET_KEY:
    raise SystemExit("❌ Missing Alpaca API credentials — set APCA_API_KEY_ID and APCA_API_SECRET_KEY.")

# ─────────────────────────────────────────────
# Alpaca Clients
# ─────────────────────────────────────────────
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

try:
    account = trading_client.get_account()
    print(f"✅ Connected — status: {account.status}, equity: ${account.equity}")
except Exception as e:
    print(f"❌ Alpaca connect error: {e}")

# ─────────────────────────────────────────────
# State (protected by a lock)
# ─────────────────────────────────────────────
lock = threading.Lock()

# pending_trades: symbol → {...}
#  - Created by BUY signal (yellow). No order yet. We watch for price ≥ entry then place a BUY limit.
pending_trades: Dict[str, Dict[str, Any]] = {}

# active_positions: symbol → {...}
#  - After order fills, we track stop and qty. We flatten on stop breach or SELL alert.
active_positions: Dict[str, Dict[str, Any]] = {}

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────
def latest_price(symbol: str) -> float:
    """Return latest executable quote price (ask if available else bid)."""
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        q = data_client.get_stock_latest_quote(req)
        px = q[symbol].ask_price or q[symbol].bid_price
        return float(px)
    except Exception as e:
        print(f"⚠️ Quote error {symbol}: {e}")
        return 0.0

def has_open_position(symbol: str) -> bool:
    try:
        for p in trading_client.get_all_positions():
            if p.symbol.upper() == symbol.upper():
                return True
    except Exception as e:
        print(f"⚠️ get_all_positions error: {e}")
    return False

def submit_limit_buy(symbol: str, qty: int, limit_px: float) -> bool:
    try:
        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            limit_price=limit_px,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order)
        print(f"✅ BUY submitted {symbol} x{qty} @ {limit_px}")
        return True
    except Exception as e:
        print(f"❌ BUY submit failed {symbol}: {e}")
        return False

def submit_limit_sell(symbol: str, qty: int, limit_px: float) -> bool:
    try:
        order = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            limit_price=limit_px,
            time_in_force=TimeInForce.DAY
        )
        trading_client.submit_order(order)
        print(f"✅ SELL submitted {symbol} x{qty} @ {limit_px}")
        return True
    except Exception as e:
        print(f"❌ SELL submit failed {symbol}: {e}")
        return False

def refresh_position_qty(symbol: str) -> int:
    """Return current position quantity for symbol (int), else 0."""
    try:
        for p in trading_client.get_all_positions():
            if p.symbol.upper() == symbol.upper():
                # p.qty is a string in alpaca-py
                return int(float(p.qty))
    except Exception as e:
        print(f"⚠️ refresh_position_qty error: {e}")
    return 0

# ─────────────────────────────────────────────
# Background watcher
#   - Polls every few seconds
#   - Triggers entry when live price crosses/equals entry (for BUY)
#   - Once position opens, watches stop (signal_low)
#   - SELL alert closes immediately (handled in webhook)
# ─────────────────────────────────────────────
def watcher_loop(poll_secs: int = 3):
    while True:
        time.sleep(poll_secs)

        with lock:
            # 1) Trigger entries for pending trades
            for symbol, trade in list(pending_trades.items()):
                entry = float(trade["entry"])
                stop = float(trade["stop"])
                qty = int(trade["qty"])
                source = trade.get("source", "YELLOW_CANDLE")

                px = latest_price(symbol)
                if px <= 0:
                    continue

                # Entry trigger: BUY when live px >= entry (next candle ticking up to/through close)
                if px >= entry:
                    print(f"▶️ Entry trigger met for {symbol}: live {px} ≥ entry {entry}")
                    # Use live ask as limit (your spec: backend quote decides where to purchase)
                    placed = submit_limit_buy(symbol, qty, px)
                    if placed:
                        # Move to active tracking (position may fill shortly)
                        active_positions[symbol] = {
                            "stop": stop,
                            "qty": qty,
                            "source": source
                        }
                        # Remove from pending
                        pending_trades.pop(symbol, None)

            # 2) Stop monitoring for active positions (flatten on breach)
            for symbol, pos in list(active_positions.items()):
                stop = float(pos["stop"])
                qty_hint = int(pos["qty"])  # we will refresh actual qty

                # If position isn't actually open yet, skip stop check until filled
                current_qty = refresh_position_qty(symbol)
                if current_qty <= 0:
                    # Not filled yet or already closed externally
                    continue

                px = latest_price(symbol)
                if px <= 0:
                    continue

                # Stop: breach if live price <= stop -> submit a SELL limit at live price
                if px <= stop:
                    print(f"🛑 STOP for {symbol}: live {px} <= stop {stop}")
                    sell_qty = current_qty
                    placed = submit_limit_sell(symbol, sell_qty, px)
                    if placed:
                        # Remove from active once we’ve sent the exit
                        active_positions.pop(symbol, None)

# Start watcher in background
threading.Thread(target=watcher_loop, args=(3,), daemon=True).start()

# ─────────────────────────────────────────────
# Webhook routes
# ─────────────────────────────────────────────
def authorize(payload: dict) -> bool:
    incoming = (payload.get("secret") or "").strip().lower()
    expected = (WEBHOOK_SECRET or "").strip().lower()
    if incoming != expected:
        print(f"❌ Unauthorized webhook — got '{incoming}', expected '{expected}'")
        return False
    return True

@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = json.loads(request.data.decode("utf-8"))
    except Exception:
        return jsonify({"error": "Invalid JSON"}), 400

    if not authorize(data):
        return jsonify({"error": "Unauthorized"}), 403

    action = (data.get("action") or "").upper()
    symbol = (data.get("ticker") or data.get("symbol") or "").upper()
    qty = int(data.get("quantity", 1))
    source = data.get("source", "UNKNOWN")

    # From indicator:
    #   entry = signal_close (your entry), stop = signal_low
    entry_price = data.get("entry_price") or data.get("signal_close")
    stop_price = data.get("stop") or data.get("signal_low")

    if not symbol or not action:
        return jsonify({"error": "Missing action or ticker"}), 400

    print(f"📩 Webhook: {symbol} {action} qty={qty} source={source}")

    with lock:
        if action == "BUY":
            if not entry_price or not stop_price:
                return jsonify({"error": "Missing entry/stop from indicator"}), 400

            entry = float(entry_price)
            stop = float(stop_price)

            # If position already open, ignore duplicate BUY
            if has_open_position(symbol):
                print(f"⚠️ Skipping BUY — position already open for {symbol}")
                return jsonify({"status": "ignored", "reason": "position already open"}), 200

            # Store/replace pending trade for this symbol
            pending_trades[symbol] = {
                "entry": entry,
                "stop": stop,
                "qty": qty,
                "side": "BUY",
                "source": source
            }
            print(f"🕒 Pending BUY set for {symbol}: entry={entry} stop={stop} qty={qty}")
            return jsonify({"status": "pending_set", "symbol": symbol, "entry": entry, "stop": stop}), 200

        elif action == "SELL":
            # SELL alert = take profit / exit NOW if we have a position
            current_qty = refresh_position_qty(symbol)
            if current_qty <= 0:
                # If there was a pending trade, cancel it
                if symbol in pending_trades:
                    pending_trades.pop(symbol, None)
                    print(f"ℹ️ SELL alert canceled pending BUY for {symbol}")
                return jsonify({"status": "no_position"}), 200

            px = latest_price(symbol)
            if px > 0:
                placed = submit_limit_sell(symbol, current_qty, px)
                if placed:
                    active_positions.pop(symbol, None)  # stop tracking after exit signal
                    return jsonify({"status": "exit_submitted", "symbol": symbol, "qty": current_qty, "price": px}), 200
                else:
                    return jsonify({"status": "error", "message": "SELL submit failed"}), 500
            else:
                return jsonify({"status": "error", "message": "No quote available"}), 500

        else:
            return jsonify({"error": "Unsupported action"}), 400

@app.route("/tv", methods=["POST"])
def tv():
    return webhook()

@app.route("/", methods=["GET"])
def home():
    return jsonify({"status": "running"}), 200

# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
























































