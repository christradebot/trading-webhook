import os
import threading
import asyncio
from functools import partial
from flask import Flask, request, jsonify
import requests

# Recommended practice: Load environment variables from a .env file locally
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # If dotenv is not installed or not needed in a deployment environment
    print("python-dotenv not found, relying on system environment variables.")

# alpaca-py import
from alpaca.data.live import StockDataStream

app = Flask(__name__)

# ===================== ENV ======================

API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")

ORDERS_URL = f"{BASE_URL}/v2/orders"
POSITIONS_URL = f"{BASE_URL}/v2/positions"

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": SECRET_KEY,
    "Content-Type": "application/json"
}

# --- Check for missing keys ---
if not API_KEY or not SECRET_KEY or not WEBHOOK_SECRET:
    print("FATAL ERROR: Missing critical environment variables. Please set APCA_API_KEY_ID, APCA_API_SECRET_KEY, and WEBHOOK_SECRET.")

trade_lock = threading.Lock()
active_trade = None # Global state for a single active trade

# ===================== HELPERS (Blocking Functions) ======================

def safe_request(method, url, **kwargs):
    """Safely executes a requests call, catching exceptions."""
    try:
        # Use a reasonable market timeout
        r = requests.request(method, url, headers=HEADERS, timeout=5, **kwargs)
        return r
    except requests.exceptions.RequestException as e:
        print(f"REQUEST ERROR [{url}]: {e}")
        return None


def place_limit_order(symbol, qty, side, price):
    """Places a limit order (Blocking Call)."""
    # Ensure qty is positive integer
    qty = int(qty)
    if qty <= 0:
        print("ORDER REJECTED: Quantity is zero or less.")
        return None, 400
        
    order = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "limit",
        "limit_price": str(round(float(price), 4)),
        "time_in_force": "day",
        "extended_hours": True
    }

    print("📤 SENDING:", order)

    r = safe_request("POST", ORDERS_URL, json=order)

    if r is None:
        return None, 500

    print("📥 ALPACA:", r.status_code, r.text)

    try:
        return r.json(), r.status_code
    except requests.exceptions.JSONDecodeError:
        # Handle non-JSON error responses
        return {"error": r.text}, r.status_code


def get_position(symbol):
    """Fetches a specific position or None (Blocking Call)."""
    r = safe_request("GET", POSITIONS_URL)

    if r is None or r.status_code != 200:
        return None

    try:
        # Iterate through all positions to find the specific symbol
        for p in r.json():
            if p["symbol"] == symbol:
                return p
    except requests.exceptions.JSONDecodeError as e:
        print(f"POSITION DECODE ERROR: {e}")
        return None

    return None


def has_position(symbol):
    """Checks if a position exists with positive quantity (Blocking Call)."""
    pos = get_position(symbol)
    if pos is None:
        return False
    # Check if the quantity is greater than a small epsilon (1e-9) to handle floating point safety
    return float(pos.get("qty", 0)) > 1e-9


def get_order_status(order_id):
    """Fetches the status of a specific order (Blocking Call)."""
    r = safe_request("GET", f"{ORDERS_URL}/{order_id}")
    if r and r.status_code == 200:
        try:
            return r.json()
        except requests.exceptions.JSONDecodeError:
            print(f"ORDER STATUS DECODE ERROR for {order_id}")
            return None
    return None


# ===================== FILL CHECK ======================

async def wait_for_fill(symbol, order_id):
    """
    Asynchronously waits for the initial buy order to fill by checking order status and position.
    Returns (entry_price, qty) or (None, None) if order fails.
    """
    print(f"⏳ Waiting for {symbol} to fill...")

    while True:

        # Check order status to see if it was rejected or canceled
        order_status = await asyncio.to_thread(get_order_status, order_id)

        if order_status and order_status.get("status") in ["rejected", "canceled", "expired"]:
            print(f"❌ Order {order_status['status'].upper()} — EXITING THREAD")
            return None, None

        # Check for position fill
        pos = await asyncio.to_thread(get_position, symbol)

        if pos and float(pos.get("qty", 0)) > 0:
            entry_price = float(pos.get("avg_entry_price", pos.get("current_price", 0.0)))
            qty = float(pos["qty"])
            print(f"✅ {symbol} FILLED @ {entry_price}")
            return entry_price, qty

        await asyncio.sleep(2)


# ====================== LADDER EXIT ======================

async def ladder_exit(symbol, start_price, hard_stop):
    """
    Attempts to exit a position by placing successively lower limit sell orders.
    It checks the remaining position quantity before each attempt to handle partial fills.
    """
    print("🪜 STARTING LADDER EXIT SEQUENCE")

    price = float(start_price)

    for i in range(6):

        # 1. Get current position qty before placing the order
        position = await asyncio.to_thread(get_position, symbol)
        
        if not position or float(position["qty"]) <= 0:
            print("✅ POSITION CLOSED")
            return
            
        qty_to_sell = float(position["qty"])
        
        # 2. Never go below hard stop
        if price < hard_stop:
            print(f"⚠️ Price {price} is below hard stop {hard_stop}. Setting price to hard stop.")
            price = hard_stop
        
        print(f"ATTEMPT {i+1} @ {price} | Qty: {qty_to_sell}")

        # 3. Place the order
        await asyncio.to_thread(place_limit_order, symbol, qty_to_sell, "sell", price)
        
        # 4. Wait for potential fill
        await asyncio.sleep(5)

        # 5. Check if we still have a position
        if not await asyncio.to_thread(has_position, symbol):
            print("✅ POSITION CLOSED")
            return

        # 6. Step down the price for the next attempt
        price = round(price - 0.01, 4)

    # Final attempt to close any remaining position
    final_position = await asyncio.to_thread(get_position, symbol)
    if final_position and float(final_position["qty"]) > 0:
        final_qty = float(final_position["qty"])
        print(f"⚠️ FINAL EXIT ATTEMPT for {final_qty} shares @ {price}")
        # Use a limit order at the final calculated price
        await asyncio.to_thread(place_limit_order, symbol, final_qty, "sell", price)
    else:
        print("Position closed during final check.")


# ====================== WEBSOCKET ======================

def start_websocket(trade):
    """
    Entry point for the WebSocket thread. Sets up the asyncio loop and handles cleanup.
    """
    global active_trade

    # Set up the event loop for this thread
    asyncio.set_event_loop(asyncio.new_event_loop())
    loop = asyncio.get_event_loop()

    async def runner():
        
        # The monitor logic is encapsulated here
        try:
            print(f"📡 SOCKET STARTED FOR {trade['symbol']}")

            # 1. Initialize data stream
            stream = StockDataStream(API_KEY, SECRET_KEY, feed="sip")

            # 2. Wait for fill
            entry_price, qty = await wait_for_fill(
                trade["symbol"],
                trade["order_id"]
            )

            if entry_price is None:
                # Order failed/canceled, monitor stops here
                return

            # --- Monitoring Variables ---
            highest = entry_price
            trail_active = False
            
            # 3. Define the main data handler
            async def on_trade(data):
                nonlocal highest, trail_active

                price = float(data.price)
                symbol = trade["symbol"]

                if price > highest:
                    highest = price

                # Activation: Activate trail after +20% from actual entry price
                if not trail_active and price >= entry_price * 1.2:
                    trail_active = True
                    print("🔥 TRAILING ACTIVATED")

                # Set base stop/target from trade data
                stop = trade["stop"]
                target = trade["target"]

                # Trailing logic
                if trail_active:
                    trail_stop = round(highest * (1 - trade["trail"] / 100), 4)
                    # Protect the stop: never move below original hard stop
                    stop = max(stop, trail_stop)

                print(f"{symbol} | PRICE: {price} | HIGH: {highest} | STOP: {stop} | TARGET: {target}")

                # EXIT logic
                if price <= stop or price >= target:
                    print("🚨 EXIT TRIGGERED")
                    # Pass the original hard stop to the ladder for protection
                    await ladder_exit(symbol, price, trade["stop"]) 
                    await stream.stop() # Stop the data stream

            # 4. Subscribe and Run
            stream.subscribe_trades(on_trade, trade["symbol"])
            await stream.run() # Use the public run method for the stream

        except Exception as e:
            print(f"CRITICAL MONITOR ERROR: {e}")
        
        finally:
            # CRITICAL: Use the lock when accessing/resetting the global state
            with trade_lock:
                print("🧹 CLEANING ACTIVE TRADE STATE")
                active_trade = None

    loop.run_until_complete(runner())


# ====================== FLASK ======================

@app.route("/")
def health():
    return "Bot Online ✅", 200


@app.route("/tv", methods=["POST"])
def webhook():
    """Handles incoming TradingView webhook."""

    global active_trade

    try:
        data = request.get_json(force=True)
        print("WEBHOOK:", data)
    except Exception as e:
        return jsonify({"error": "Bad JSON input", "details": str(e)}), 400

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    # --- Data Parsing and Validation ---
    try:
        symbol = data["ticker"]
        # Convert quantity to integer safely
        qty    = int(data["quantity"]) 
        entry  = float(data["entry"])
        stop   = float(data["stop"])
        target = float(data["target"])
        # Use .get with a default value for optional 'trail'
        trail  = float(data.get("trail", 15)) 
        
        if not all([symbol, qty, entry, stop, target]) or qty <= 0:
             return jsonify({"error": "Missing required fields or invalid quantity (<= 0) in webhook data"}), 400
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": "Invalid or missing fields in webhook data", "details": str(e)}), 400
    # --- End Validation ---

    with trade_lock:
        
        # Enforce single active trade
        if active_trade is not None:
            return jsonify({"error": f"Trade for {active_trade['symbol']} is already running. Rejecting new trade for {symbol}."}), 429

        # 1. Place the initial limit order
        order_json, status = place_limit_order(symbol, qty, "buy", entry)

        if status not in [200, 201] or "id" not in order_json:
            return jsonify({"error": "Buy order rejected", "alpaca": order_json}), 500
        
        initial_order_id = order_json["id"]

        # 2. Store trade details and set global state
        active_trade = {
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "stop": stop,
            "target": target,
            "trail": trail,
            "order_id": initial_order_id # Store the order ID for monitoring the fill
        }

        # 3. Start the WebSocket monitoring thread
        t = threading.Thread(
            target=start_websocket,
            args=(active_trade,),
            daemon=True # Daemon threads exit when the main program exits
        )
        t.start()
        
    return jsonify({"msg": f"{symbol} trade initiated and monitoring started. Order ID: {initial_order_id}"}), 200


if __name__ == "__main__":
    print("Starting Flask application. Ensure environment variables are set.")
    # Use Gunicorn in production for better performance and stability
    # E.g., gunicorn -w 4 -b 0.0.0.0:8080 app:app
    app.run(host="0.0.0.0", port=8080)























































































































