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
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

app = Flask(__name__)

# --- Global State and Constants ---
POSITIONS = {} # {symbol: {avg_entry: float, qty: float, stop_loss: float, add_used: bool, pnl_percent: float, source: str}}
SECRET_KEY_CHECK = "chrisbot1501"
# Risk Management Constants
MAX_DAILY_TRADES = 10
MAX_CONCURRENT_POSITIONS = 3
RANGE_GUARD_THRESHOLD = 0.11 # 11.0% max range (Low to Close)
OPEN_WINDOW_START = dt_time(9, 30, 0) # 9:30 AM ET
OPEN_WINDOW_END = dt_time(9, 45, 0)   # 9:45 AM ET

# --- Utility Functions ---

def log(message):
    """Simple logging with timestamp."""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}")

def round_tick(price):
    """Rounds price to the nearest cent."""
    return round(price, 2)

def nfloat(x):
    """Safely converts a string to a float, returning None on failure."""
    try:
        # CRITICAL: Ensures that if the variable substitution failed (like {{close}}), 
        # it returns None instead of raising an unhandled exception.
        return None if x in ("", None) else float(x)
    except ValueError:
        return None
    except TypeError:
        return None

def get_market_time():
    """Returns the current market time (using Alpaca status or system time as fallback)."""
    # NOTE: In a real environment, you'd use Alpaca's clock API to get the *exact* market time.
    # For this simulated environment, we'll use local time but note it's approximate.
    # Assuming this environment is running in UTC, we approximate ET (UTC-5/UTC-4)
    now_utc = datetime.utcnow()
    # Assuming standard EST (UTC-5) for simplicity, adjust for DST if needed
    market_time = now_utc - timedelta(hours=5) 
    return market_time.time()

def in_open_window():
    """Checks if the current market time is between 9:30 AM and 9:45 AM ET."""
    current_time = get_market_time()
    return OPEN_WINDOW_START <= current_time <= OPEN_WINDOW_END

def get_position(symbol):
    """Fetches the current Alpaca position and updates the global POSITIONS state."""
    try:
        alpaca_position = trading_client.get_open_position(symbol)
        avg_entry = float(alpaca_position.avg_entry_price)
        qty = float(alpaca_position.qty)
        pnl_percent = float(alpaca_position.unrealized_plpc) * 100
        
        # Check if we already track this position globally and merge data
        if symbol in POSITIONS:
            POSITIONS[symbol].update({
                'avg_entry': avg_entry,
                'qty': qty,
                'pnl_percent': pnl_percent,
            })
        else:
             # If a position exists on Alpaca but not in POSITIONS, add a basic entry
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
        # Position does not exist or API error
        if symbol in POSITIONS:
            del POSITIONS[symbol]
        return None

def cancel_all_orders(symbol):
    """Cancels all open orders for a given symbol."""
    try:
        trading_client.cancel_orders(symbol)
        log(f"🧹 Canceled all open orders for {symbol}.")
    except Exception as e:
        log(f"⚠️ Failed to cancel orders for {symbol}: {e}")

# --- Core Logic ---

def compute_stop(entry, signal_low):
    """Calculates the stop-loss price based on time window and signal low."""
    market_time = get_market_time()
    lo = nfloat(signal_low)
    
    # 9:30 AM - 9:45 AM Rule: Min of Signal Low or 3% Hard Floor
    if OPEN_WINDOW_START <= market_time <= OPEN_WINDOW_END:
        # Use the minimum of the technical stop (Signal Low) or the 3% hard floor.
        # If signal_low is missing, we default to the 3% hard floor.
        hard_floor = entry * 0.97
        if lo is not None and lo > 0:
            stop = min(lo, hard_floor)
        else:
            stop = hard_floor
            
        log(f"⏰ Open Window Stop ({market_time.strftime('%H:%M')}): Entry={entry}, SignalLow={lo}, 3%Floor={hard_floor:.2f} → Stop={stop:.3f}")
        return round_tick(stop)
    
    # After 9:45 AM Rule: Use Signal Low only (3% floor is gone)
    else:
        # If signal_low is missing outside the open window, no stop is set (relying on target/manual exit).
        if lo is not None and lo > 0:
            return round_tick(lo)
        return None

def aggressive_close(symbol, price_ref, reason):
    """
    Executes a market close using aggressive limit orders to ensure quick fill.
    CRITICAL FIX: Checks available position quantity before placing each order.
    """
    log(f"🏃 Closing {symbol} aggressively at {price_ref} | Reason={reason}")
    cancel_all_orders(symbol)
    
    # Initial check (mostly for logging)
    position_data = get_position(symbol)
    if not position_data or position_data.get('qty', 0) <= 0:
        log(f"ℹ️ {symbol} is already flat, aggressive close halted.")
        return

    MAX_ATTEMPTS = 20
    
    for i in range(1, MAX_ATTEMPTS + 1):
        # CRITICAL FIX: Fetch the current position data on every iteration
        position_data = get_position(symbol)
        current_qty = position_data.get('qty', 0)
        
        if current_qty <= 0:
            log(f"✅ Position {symbol} successfully flat after {i-1} attempts.")
            break
        
        # Get the current quote to set a limit order just below the bid/last
        try:
            quote = trading_client.get_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol]))
            best_bid = quote[symbol].bid_price
            
            # Use the best bid price to attempt to fill immediately, slightly undercutting it by a tick
            aggressive_limit_price = round_tick(best_bid - 0.01) # Undercut by one cent for quick fill
            
            if aggressive_limit_price <= 0.01:
                # If price is too low, use Market Order as a final resort
                 trading_client.close_position(symbol, ClosePositionRequest(percentage=100))
                 log(f"⚠️ {symbol} price too low ({best_bid}). Market closing as final resort.")
                 break

            order_data = LimitOrderRequest(
                symbol=symbol,
                qty=current_qty, # CRITICAL: Use the current quantity
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=aggressive_limit_price
            )
            trading_client.submit_order(order_data)
            log(f"📥 SELL LIMIT {symbol} x{current_qty} @ {aggressive_limit_price} | Aggressive exit {i}/{MAX_ATTEMPTS} ({reason})")

        except Exception as e:
            log(f"❌ SELL limit error {symbol} @{aggressive_limit_price}: {e}")
            # If the error is insufficient qty, we break out as the position is likely closed
            if "insufficient qty" in str(e).lower():
                break

        time.sleep(2) # Wait 2 seconds between attempts

    # Final Check and Cleanup
    final_pos_data = get_position(symbol)
    if final_pos_data and final_pos_data.get('qty', 0) > 0:
        log(f"❗ Failed to fully flat {symbol}. Remaining Qty: {final_pos_data['qty']}")
    
    # Remove position from tracking after exit attempt
    if symbol in POSITIONS:
        pnl = final_pos_data.get('pnl_percent', 0.0) if final_pos_data else 0.0
        log(f"💰 EXIT {symbol} completed | reason={reason} | PnL%≈{pnl:.2f}")
        del POSITIONS[symbol]


def watch_loop(symbol, stop):
    """Monitors price in a background thread and triggers aggressive close if stop is breached."""
    log(f"🚀 Started new stop watcher for {symbol}")
    
    # Update global state with the calculated stop loss
    if symbol in POSITIONS:
        POSITIONS[symbol]['stop_loss'] = stop

    market_is_open = True
    
    while symbol in POSITIONS and POSITIONS[symbol].get('qty', 0) > 0 and market_is_open:
        try:
            position_stop = POSITIONS[symbol]['stop_loss']
            if position_stop is None:
                # If the stop is NONE (due to missing signal_low after 9:45), we only continue
                # if the user hasn't explicitly set a target (i.e., we rely on target alerts).
                time.sleep(5)
                continue
                
            # Get latest quote
            quote = trading_client.get_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol]))
            # Use the bid price as the reference for hitting the stop (safest check for longs)
            ref = quote[symbol].bid_price 

            if ref is not None and ref <= float(position_stop):
                log(f"🛑 Stop breach {symbol}: last={ref:.5f} ≤ stop={position_stop:.5f} → aggressive close")
                aggressive_close(symbol, ref, reason="STOP")
                break
            
            time.sleep(1) # Check price every second

        except Exception as e:
            log(f"⚠️ Stop watcher error for {symbol}: {e}")
            time.sleep(5) # Wait longer on error

    log(f"🧹 Stop watcher ended for {symbol}")
    # Clear stop_loss from global state on exit (this is mainly defensive now, as aggressive_close handles final removal)
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

    # --- Pre-Checks ---
    if symbol in POSITIONS and action == "BUY":
        log(f"ℹ️ {symbol} BUY ignored; already in a position.")
        return 
    
    if action == "ADD" and (symbol not in POSITIONS or POSITIONS[symbol]['add_used']):
        log(f"ℹ️ {symbol} ADD ignored; not in position or ADD already used.")
        return 
    
    if entry_price is None:
        log(f"❌ {symbol} {action} failed: Missing valid entry_price.")
        return

    # --- Range Guard (Only for Initial BUY and Hammer/Engulfing) ---
    if action == "BUY" and source in ["HAMMER_ENGULFING", "ITG_SCALPER"]:
        if signal_low is None or signal_close is None:
            log(f"ℹ️ Range guard skipped (no valid signal_low/signal_close).")
        else:
            price_range = (signal_close - signal_low) / signal_low
            log(f"🔎 Low→Close range: {price_range:.2%} (≤ {RANGE_GUARD_THRESHOLD:.1%} required)")
            if price_range > RANGE_GUARD_THRESHOLD:
                log(f"🚫 {symbol} BUY blocked by {RANGE_GUARD_THRESHOLD:.1%} range guard.")
                return

    # --- Position Profit Guard (Only for ADD) ---
    if action == "ADD":
        pos_data = POSITIONS[symbol]
        current_price = nfloat(trading_client.get_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol]))[symbol].bid_price)
        
        if current_price is None or current_price <= pos_data['avg_entry']:
            log(f"🚫 {symbol} ADD blocked: Not in profit (current={current_price}, avg={pos_data['avg_entry']}).")
            return 
    
    # --- Execute Order ---
    try:
        order_data = LimitOrderRequest(
            symbol=symbol,
            qty=quantity,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.GTC,
            limit_price=entry_price
        )
        trading_client.submit_order(order_data)

        # Update global POSITIONS state for tracking
        stop_loss = compute_stop(entry_price, signal_low)
        
        if symbol not in POSITIONS:
            # New position tracking
            POSITIONS[symbol] = {
                'avg_entry': entry_price, 
                'qty': 0, # Will be updated by get_position() in the watch loop
                'stop_loss': stop_loss, 
                'add_used': (action == "ADD"), 
                'source': source
            }
        elif action == "ADD":
            POSITIONS[symbol]['add_used'] = True

        log(f"📥 BUY LIMIT {symbol} x{quantity} @ {entry_price}")
        log(f"✅ {symbol} {action} placed @ {entry_price} | stop={stop_loss} | src={source}")

        # Start the stop-loss watcher thread immediately
        threading.Thread(target=watch_loop, args=(symbol, stop_loss)).start()

    except Exception as e:
        log(f"❌ Failed to submit {action} order for {symbol}: {e}")

def check_and_place_exit(data):
    """Processes EXIT actions (Target hit or manual close)."""
    symbol = data['ticker']
    exit_price = nfloat(data['exit_price'])

    pos_data = get_position(symbol)
    if not pos_data:
        log(f"ℹ️ {symbol} EXIT ignored; flat.")
        return

    # Cancel all stop-watchers and open orders
    cancel_all_orders(symbol)
    
    # Case 1: Target Price is Provided
    if exit_price is not None:
        log(f"🔔 {symbol} EXIT try alert target @{exit_price}")

        try:
            # Try to place a limit order at the target price
            order_data = LimitOrderRequest(
                symbol=symbol,
                qty=pos_data['qty'],
                side=OrderSide.SELL,
                time_in_force=TimeInForce.GTC,
                limit_price=exit_price
            )
            trading_client.submit_order(order_data)
            
            # Wait a short time for the limit order to fill
            time.sleep(6) 
            
            # If still not flat, use aggressive close as fallback
            pos_data = get_position(symbol)
            if pos_data and pos_data.get('qty', 0) > 0:
                log(f"⏱ {symbol} target limit failed. Starting aggressive exit as fallback.")
                aggressive_close(symbol, exit_price, reason="EXIT_ALERT_FALLBACK")
                
        except Exception as e:
            log(f"❌ Failed to submit target limit order for {symbol}: {e}")
            # Fall back to aggressive market close if limit order fails
            aggressive_close(symbol, exit_price, reason="EXIT_ALERT_TARGET_FAIL")

    # Case 2: No Target Price Provided (Immediate Market/Aggressive Close)
    else:
        # Trigger immediate aggressive close at current market price (best bid)
        log(f"🔔 {symbol} EXIT received with no price. Starting aggressive market close.")
        try:
            quote = trading_client.get_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=[symbol]))
            current_ref_price = quote[symbol].bid_price
            aggressive_close(symbol, current_ref_price, reason="EXIT_ALERT_MARKET")
        except Exception as e:
            log(f"❌ Failed to get quote for aggressive market exit: {e}")
            # Last resort: use Alpaca's simple close
            trading_client.close_position(symbol, ClosePositionRequest(percentage=100))
            log(f"⚠️ {symbol} simple market close triggered.")
            
    # Final cleanup log - Aggressive_close handles final cleanup and removal from POSITIONS


# --- Flask Webhook Endpoint ---
@app.route("/tv", methods=["POST"])
def webhook_handler():
    """Receives webhook signals from TradingView."""
    try:
        data = request.get_json(force=True)
        
        # --- Authentication and Validation ---
        if data.get("secret") != SECRET_KEY_CHECK:
            log("🚫 Unauthorized access attempt.")
            return jsonify({"status": "error", "message": "Unauthorized"}), 401

        action = data.get("action")
        symbol = data.get("ticker")
        
        if not action or not symbol:
            log("❌ Invalid payload: Missing action or ticker.")
            return jsonify({"status": "error", "message": "Missing action/ticker"}), 400

        log(f"📡 Received {action} signal for {symbol} | Source: {data.get('source')}")

        if action in ["BUY", "ADD"]:
            check_and_place_buy(data)
        elif action == "EXIT":
            check_and_place_exit(data)
        else:
            log(f"⚠️ Unknown action received: {action}")
            return jsonify({"status": "error", "message": "Unknown action"}), 400

        return jsonify({"status": "ok", "message": f"{action} signal for {symbol} processed"}), 200

    except Exception as e:
        log(f"❌ Webhook processing error: {e}. Data: {request.data}")
        return jsonify({"status": "error", "message": str(e)}), 500

# --- Market Time Logging for Debugging ---
def log_market_open():
    """Logs when the open window starts and ends."""
    current_date = datetime.now().date()
    
    # Calculate timestamps in UTC, then convert to market time for comparison
    def get_market_datetime(time_obj):
        # We must use UTC because that is what datetime.utcnow() returns
        dt_utc = datetime.combine(current_date, time_obj) + timedelta(hours=5) 
        return dt_utc

    open_start_utc = get_market_datetime(OPEN_WINDOW_START)
    open_end_utc = get_market_datetime(OPEN_WINDOW_END)
    
    # Simple loop for logging events
    while True:
        now = datetime.utcnow()
        
        # Check for open window start
        if now.hour == open_start_utc.hour and now.minute == open_start_utc.minute and now.second < 10:
            log("🟢 Market Open Window STARTED (9:30 AM ET)")
        
        # Check for open window end
        elif now.hour == open_end_utc.hour and now.minute == open_end_utc.minute and now.second < 10:
            log("🔴 Market Open Window ENDED (9:45 AM ET)")
            
        time.sleep(10) # Check every 10 seconds

# Start the market time logger in a background thread
threading.Thread(target=log_market_open, daemon=True).start()

if __name__ == "__main__":
    # In a production environment, use gunicorn or other WSGI server
    app.run(host="0.0.0.0", port=8080)

















































