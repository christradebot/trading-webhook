import json
import time
import os
import requests
import threading
from datetime import datetime, time as dt_time, timedelta
from flask import Flask, request, jsonify
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# --- Configuration ---
# Fetch credentials from environment variables
API_KEY = os.environ.get("ALPACA_API_KEY")
SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
BASE_URL = os.environ.get("ALPACA_BASE_URL", "https://api.alpaca.markets")

if not all([API_KEY, SECRET_KEY, BASE_URL]):
    print("FATAL: Alpaca API credentials not set in environment variables.")
    # Exit silently if run in an environment without keys
    exit(1)

# Initialize Alpaca Clients
# Note: paper=True routes to paper automatically; BASE_URL is kept for logging/reference.
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

app = Flask(__name__)

# --- Global State and Constants ---
# {symbol: {avg_entry: float, qty: float, stop_loss: float, add_used: bool, pnl_percent: float, source: str}}
POSITIONS = {}
SECRET_KEY_CHECK = "chrisbot1501"

# Risk Management Constants
MAX_DAILY_TRADES = 10
MAX_CONCURRENT_POSITIONS = 3
RANGE_GUARD_THRESHOLD = 0.11  # 11.0% max range (Low to Close)
OPEN_WINDOW_START = dt_time(9, 30, 0)  # 9:30 AM ET
OPEN_WINDOW_END = dt_time(9, 45, 0)    # 9:45 AM ET

# --- Utility Functions ---

def log(message):
    """Simple logging with timestamp."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)

def round_tick(price):
    """Rounds price to the nearest cent."""
    return round(price, 2)

def nfloat(x):
    """Safely converts a string to a float, returning None on failure."""
    try:
        # If TradingView variable substitution failed (like {{close}}), return None.
        return None if x in ("", None) else float(x)
    except (ValueError, TypeError):
        return None

def get_market_time():
    """Returns the current market time (approximate ET)."""
    # For accuracy you'd call Alpaca clock; this is a simple UTC->ET conversion.
    now_utc = datetime.utcnow()
    market_time = now_utc - timedelta(hours=5)  # naive EST/EDT approximation
    return market_time.time()

def in_open_window():
    """Checks if current market time is between 9:30 and 9:45 AM ET."""
    current_time = get_market_time()
    return OPEN_WINDOW_START <= current_time <= OPEN_WINDOW_END

def get_position(symbol):
    """Fetches current Alpaca position and updates POSITIONS."""
    try:
        alpaca_position = trading_client.get_open_position(symbol)
        avg_entry = float(alpaca_position.avg_entry_price)
        qty = float(alpaca_position.qty)
        pnl_percent = float(alpaca_position.unrealized_plpc) * 100

        if symbol in POSITIONS:
            POSITIONS[symbol].update({
                'avg_entry': avg_entry,
                'qty': qty,
                'pnl_percent': pnl_percent,
            })
        else:
            POSITIONS[symbol] = {
                'avg_entry': avg_entry,
                'qty': qty,
                'pnl_percent': pnl_percent,
                'stop_loss': None,
                'add_used': False,
                'source': 'EXTERNAL'
            }
        return POSITIONS[symbol]
    except Exception:
        if symbol in POSITIONS:
            del POSITIONS[symbol]
        return None

def cancel_all_orders(symbol):
    """Cancels all open orders for a given symbol."""
    try:
        # alpaca-py has cancel_orders() (all); symbol-specific cancel isn't official.
        # If symbol-scoped cancel raises, fall back to cancel all.
        try:
            trading_client.cancel_orders(symbol)
        except TypeError:
            trading_client.cancel_orders()
        log(f"🧹 Canceled open orders for {symbol}.")
    except Exception as e:
        log(f"⚠️ Failed to cancel orders for {symbol}: {e}")

# --- Core Logic ---

def compute_stop(entry, signal_low):
    """Calculates the stop-loss based on window and signal low."""
    market_time = get_market_time()
    lo = nfloat(signal_low)

    # 9:30–9:45: min(signal low, 3% hard floor). If missing, use 3% floor.
    if OPEN_WINDOW_START <= market_time <= OPEN_WINDOW_END:
        hard_floor = entry * 0.97
        if lo is not None and lo > 0:
            stop = min(lo, hard_floor)
        else:
            stop = hard_floor
        log(f"⏰ Open Window Stop: Entry={entry}, SignalLow={lo}, 3%Floor={hard_floor:.2f} → Stop={stop:.3f}")
        return round_tick(stop)

    # After 9:45: use signal low only
    if lo is not None and lo > 0:
        return round_tick(lo)
    return None

def aggressive_close(symbol, price_ref, reason):
    """
    Executes a market-leaning close using aggressive limit orders.
    Re-checks available qty each loop.
    """
    log(f"🏃 Closing {symbol} aggressively at {price_ref} | Reason={reason}")
    cancel_all_orders(symbol)

    position_data = get_position(symbol)
    if not position_data or position_data.get('qty', 0) <= 0:
        log(f"ℹ️ {symbol} is already flat, aggressive close halted.")
        return

    MAX_ATTEMPTS = 20
    for i in range(1, MAX_ATTEMPTS + 1):
        position_data = get_position(symbol)
        current_qty = position_data.get('qty', 0) if position_data else 0
        if current_qty <= 0:
            log(f"✅ Position {symbol} successfully flat after {i-1} attempts.")
            break

        try:
            quote = trading_client.get_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol]))
            best_bid = quote[symbol].bid_price
            aggressive_limit_price = round_tick(best_bid - 0.01) if best_bid is not None else None

            if aggressive_limit_price is None or aggressive_limit_price <= 0.01:
                trading_client.close_position(symbol, ClosePositionRequest(percentage=100))
                log(f"⚠️ {symbol} price too low/None ({best_bid}). Market closing as final resort.")
                break

            order_data = LimitOrderRequest(
                symbol=symbol,
                qty=current_qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=aggressive_limit_price
            )
            trading_client.submit_order(order_data)
            log(f"📥 SELL LIMIT {symbol} x{current_qty} @ {aggressive_limit_price} | Aggressive exit {i}/{MAX_ATTEMPTS} ({reason})")

        except Exception as e:
            log(f"❌ SELL limit error {symbol}: {e}")
            if "insufficient qty" in str(e).lower():
                break

        time.sleep(2)

    final_pos_data = get_position(symbol)
    if final_pos_data and final_pos_data.get('qty', 0) > 0:
        log(f"❗ Failed to fully flat {symbol}. Remaining Qty: {final_pos_data['qty']}")

    if symbol in POSITIONS:
        pnl = final_pos_data.get('pnl_percent', 0.0) if final_pos_data else 0.0
        log(f"💰 EXIT {symbol} completed | reason={reason} | PnL%≈{pnl:.2f}")
        del POSITIONS[symbol]

def watch_loop(symbol, stop):
    """Monitors price and triggers aggressive close if stop is breached."""
    log(f"🚀 Started new stop watcher for {symbol}")

    if symbol in POSITIONS:
        POSITIONS[symbol]['stop_loss'] = stop

    market_is_open = True
    while symbol in POSITIONS and POSITIONS[symbol].get('qty', 0) > 0 and market_is_open:
        try:
            position_stop = POSITIONS[symbol]['stop_loss']
            if position_stop is None:
                time.sleep(5)
                continue

            quote = trading_client.get_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol]))
            ref = quote[symbol].bid_price

            if ref is not None and ref <= float(position_stop):
                log(f"🛑 Stop breach {symbol}: last={ref:.5f} ≤ stop={position_stop:.5f} → aggressive close")
                aggressive_close(symbol, ref, reason="STOP")
                break

            time.sleep(1)

        except Exception as e:
            log(f"⚠️ Stop watcher error for {symbol}: {e}")
            time.sleep(5)

    log(f"🧹 Stop watcher ended for {symbol}")
    if symbol in POSITIONS and POSITIONS[symbol].get('stop_loss') is not None:
        POSITIONS[symbol]['stop_loss'] = None

def check_and_place_buy(data):
    """Processes BUY and ADD actions with all guards."""
    symbol = data['ticker']
    action = data['action']
    quantity = int(data['quantity'])
    entry_price = nfloat(data['entry_price'])
    signal_low = nfloat(data['signal_low'])
    signal_close = nfloat(data['signal_close'])
    source = data['source']

    # Pre-checks
    if symbol in POSITIONS and action == "BUY":
        log(f"ℹ️ {symbol} BUY ignored; already in a position.")
        return

    if action == "ADD" and (symbol not in POSITIONS or POSITIONS[symbol]['add_used']):
        log(f"ℹ️ {symbol} ADD ignored; not in position or ADD already used.")
        return

    if entry_price is None:
        log(f"❌ {symbol} {action} failed: Missing valid entry_price.")
        return

    # Range guard
    if action == "BUY" and source in ["HAMMER_ENGULFING", "ITG_SCALPER"]:
        if signal_low is None or signal_close is None:
            log("ℹ️ Range guard skipped (no valid signal_low/signal_close).")
        else:
            price_range = (signal_close - signal_low) / signal_low
            log(f"🔎 Low→Close range: {price_range:.2%} (≤ {RANGE_GUARD_THRESHOLD:.1%} required)")
            if price_range > RANGE_GUARD_THRESHOLD:
                log(f"🚫 {symbol} BUY blocked by {RANGE_GUARD_THRESHOLD:.1%} range guard.")
                return

    # ADD must be in profit
    if action == "ADD":
        pos_data = POSITIONS[symbol]
        try:
            current_price = nfloat(
                trading_client.get_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol]))[symbol].bid_price
            )
        except Exception as e:
            log(f"⚠️ Failed to fetch current bid for ADD on {symbol}: {e}")
            current_price = None

        if current_price is None or current_price <= pos_data['avg_entry']:
            log(f"🚫 {symbol} ADD blocked: Not in profit (current={current_price}, avg={pos_data['avg_entry']}).")
            return

    # Execute order
    try:
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=entry_price
        )
        trading_client.submit_order(order_data)

        stop_loss = compute_stop(entry_price, signal_low)

        if symbol not in POSITIONS:
            POSITIONS[symbol] = {
                'avg_entry': entry_price,
                'qty': 0,  # updated later by get_position()
                'stop_loss': stop_loss,
                'add_used': (action == "ADD"),
                'source': source
            }
        elif action == "ADD":
            POSITIONS[symbol]['add_used'] = True

        log(f"📥 BUY LIMIT {symbol} x{quantity} @ {entry_price}")
        log(f"✅ {symbol} {action} placed @ {entry_price} | stop={stop_loss} | src={source}")

        threading.Thread(target=watch_loop, args=(symbol, stop_loss), daemon=True).start()

    except Exception as e:
        log(f"❌ Failed to submit {action} order for {symbol}: {e}")

def check_and_place_exit(data):
    """Processes EXIT actions (Target hit or manual close)."""
    symbol = data['ticker']
    exit_price = nfloat(data.get('exit_price'))

    pos_data = get_position(symbol)
    if not pos_data:
        log(f"ℹ️ {symbol} EXIT ignored; flat.")
        return

    cancel_all_orders(symbol)

    if exit_price is not None:
        log(f"🔔 {symbol} EXIT try alert target @{exit_price}")
        try:
            order_data = LimitOrderRequest(
                symbol=symbol,
                qty=pos_data['qty'],
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=exit_price
            )
            trading_client.submit_order(order_data)

            time.sleep(6)

            pos_data = get_position(symbol)
            if pos_data and pos_data.get('qty', 0) > 0:
                log(f"⏱ {symbol} target limit not filled. Starting aggressive exit fallback.")
                aggressive_close(symbol, exit_price, reason="EXIT_ALERT_FALLBACK")

        except Exception as e:
            log(f"❌ Failed to submit target limit order for {symbol}: {e}")
            aggressive_close(symbol, exit_price, reason="EXIT_ALERT_TARGET_FAIL")

    else:
        log(f"🔔 {symbol} EXIT received with no price. Starting aggressive market close.")
        try:
            quote = trading_client.get_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol]))
            current_ref_price = quote[symbol].bid_price
            aggressive_close(symbol, current_ref_price, reason="EXIT_ALERT_MARKET")
        except Exception as e:
            log(f"❌ Failed to get quote for aggressive market exit: {e}")
            trading_client.close_position(symbol, ClosePositionRequest(percentage=100))
            log(f"⚠️ {symbol} simple market close triggered.")

# --- Async wrapper for webhook work ---

def process_signal(data):
    """Fire-and-forget processing so /tv can return immediately."""
    try:
        action = data.get("action")
        if action in ["BUY", "ADD"]:
            check_and_place_buy(data)
        elif action == "EXIT":
            check_and_place_exit(data)
        else:
            log(f"⚠️ Unknown action received in background worker: {action}")
    except Exception as e:
        log(f"❌ Background process_signal error: {e}")

# --- Flask Webhook Endpoint ---

@app.route("/tv", methods=["POST"])
def webhook_handler():
    """Receives webhook signals from TradingView."""
    try:
        data = request.get_json(force=True) or {}

        # Authentication and validation (fast path)
        if data.get("secret") != SECRET_KEY_CHECK:
            log("🚫 Unauthorized access attempt.")
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        action = data.get("action")
        symbol = data.get("ticker")
        if not action or not symbol:
            log("❌ Invalid payload: Missing action or ticker.")
            return jsonify({"status": "error", "message": "Missing action/ticker"}), 400

        log(f"📡 Received {action} signal for {symbol} | Source: {data.get('source')}")

        # ✅ Immediately return and do the heavy work in background to avoid 499 timeouts
        threading.Thread(target=process_signal, args=(data,), daemon=True).start()
        return jsonify({"status": "ok", "message": "Signal received"}), 200

    except Exception as e:
        log(f"❌ Webhook processing error: {e}. Data: {request.data}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- Market Time Logging for Debugging ---

def log_market_open():
    """Logs when the open window starts and ends."""
    current_date = datetime.now().date()

    def get_market_datetime(time_obj):
        # Convert ET time to approximate UTC for comparison with datetime.utcnow()
        dt_utc = datetime.combine(current_date, time_obj) + timedelta(hours=5)
        return dt_utc

    open_start_utc = get_market_datetime(OPEN_WINDOW_START)
    open_end_utc = get_market_datetime(OPEN_WINDOW_END)

    while True:
        now = datetime.utcnow()
        if now.hour == open_start_utc.hour and now.minute == open_start_utc.minute and now.second < 10:
            log("🟢 Market Open Window STARTED (9:30 AM ET)")
        elif now.hour == open_end_utc.hour and now.minute == open_end_utc.minute and now.second < 10:
            log("🔴 Market Open Window ENDED (9:45 AM ET)")
        time.sleep(10)

# Start the market time logger in a background thread
threading.Thread(target=log_market_open, daemon=True).start()

if __name__ == "__main__":
    # In production, gunicorn runs this; this path is for local testing.
    app.run(host="0.0.0.0", port=8080)


















































