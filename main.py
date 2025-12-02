import os
import threading
import time
import asyncio
from flask import Flask, request, jsonify
import requests

try:
    # Local dev convenience; on Railway env vars are already set
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from alpaca.data.live import StockDataStream
from alpaca.data.enums import DataFeed # IMPORTANT: fixes 'str has no attribute value' bug

app = Flask(__name__)

# ===================== ENV =====================

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

if not API_KEY or not SECRET_KEY or not WEBHOOK_SECRET:
    print("FATAL: Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY / WEBHOOK_SECRET")

trade_lock = threading.Lock()
active_trade = None # single active trade dict


# ===================== BASIC REST HELPERS =====================

def safe_request(method, url, **kwargs):
    """Wrapper around requests with timeout and error logging."""
    try:
        r = requests.request(method, url, headers=HEADERS, timeout=5, **kwargs)
        return r
    except requests.RequestException as e:
        print(f"REQUEST ERROR {method} {url}: {e}")
        return None


def place_limit_order(symbol, qty, side, price):
    """Place a plain limit order (blocking). Returns (json, status_code)."""
    qty = int(qty)
    if qty <= 0:
        print("ORDER REJECTED: qty <= 0")
        return {"error": "qty <= 0"}, 400

    # tick size 0.01 always
    px = round(float(price), 2)

    order = {
        "symbol": symbol,
        "qty": qty,
        "side": side,
        "type": "limit",
        "limit_price": str(px),
        "time_in_force": "day",
        "extended_hours": True
    }

    print(f"ORDER → {side.upper()} {symbol} @ {px} (qty={qty})")
    r = safe_request("POST", ORDERS_URL, json=order)
    if r is None:
        return {"error": "request failed"}, 500

    print("ALPACA:", r.status_code, r.text)
    try:
        return r.json(), r.status_code
    except ValueError:
        return {"error": r.text}, r.status_code


def get_positions():
    r = safe_request("GET", POSITIONS_URL)
    if r is None or r.status_code != 200:
        return []
    try:
        return r.json()
    except ValueError:
        return []


def get_position(symbol):
    for p in get_positions():
        if p.get("symbol") == symbol:
            return p
    return None


def has_position(symbol):
    pos = get_position(symbol)
    if not pos:
        return False
    try:
        return float(pos.get("qty", 0)) > 0
    except (TypeError, ValueError):
        return False


def cancel_open_orders_for_symbol(symbol, side=None):
    """Cancel all open orders for symbol (optionally only one side)."""
    r = safe_request("GET", ORDERS_URL, params={"status": "open", "limit": 200})
    if r is None or r.status_code != 200:
        return

    try:
        orders = r.json()
    except ValueError:
        return

    for o in orders:
        if o.get("symbol") != symbol:
            continue
        if side and o.get("side") != side:
            continue
        oid = o.get("id")
        if not oid:
            continue
        safe_request("DELETE", f"{ORDERS_URL}/{oid}")
        print(f"Cancel open {side or 'any'} order: {symbol} {oid}")


def get_last_filled_order(symbol, side):
    """Return most recent closed order for symbol+side, or None."""
    r = safe_request("GET", ORDERS_URL, params={"status": "closed", "limit": 50, "direction": "desc"})
    if r is None or r.status_code != 200:
        return None

    try:
        orders = r.json()
    except ValueError:
        return None

    for o in orders:
        if o.get("symbol") == symbol and o.get("side") == side and o.get("filled_qty") not in (None, "0"):
            return o
    return None


# ===================== LADDER BUY (BLOCKING, NO WEBSOCKET) =====================

def ladder_buy(symbol, qty, entry_price):
    """
    Ladder BUY: 6 steps, 5 seconds each.
    Prices: entry, entry+0.01, ..., entry+0.05.
    Stops as soon as a position exists. Cancels remaining open buys.
    Returns (fill_entry_price, filled_qty) or (None, 0).
    """
    base = float(entry_price)
    print(f"LADDER BUY START for {symbol} from {base}")

    for step in range(6):
        price = round(base + 0.01 * step, 2)

        # If already have a position, just stop laddering
        if has_position(symbol):
            break

        order_json, status = place_limit_order(symbol, qty, "buy", price)
        if status not in (200, 201):
            print(f"LADDER BUY step {step+1}: order rejected: {order_json}")
            # We still wait and check, in case something partial went through.
        else:
            print(f"LADDER BUY step {step+1}: placed @ {price}")

        time.sleep(5)

        if has_position(symbol):
            print("LADDER BUY: position detected, stopping ladder")
            break

    # After laddering: cancel remaining open buy orders
    cancel_open_orders_for_symbol(symbol, side="buy")

    pos = get_position(symbol)
    if pos and float(pos.get("qty", 0)) > 0:
        filled_qty = float(pos["qty"])
        # Use avg_entry_price if present
        entry = float(pos.get("avg_entry_price", pos.get("current_price", base)))
        print(f"BUY FILLED: {symbol} qty={filled_qty} entry={entry}")
        return entry, filled_qty

    print("LADDER BUY: no fill, trade aborted")
    return None, 0.0


# ===================== LADDER EXIT (ASYNC, USED BY WEBSOCKET) =====================

async def ladder_exit(symbol, original_stop, last_price_seen):
    """
    Ladder EXIT: 6 steps, 5s each, then aggressive limit.
    - All exits are LIMIT orders (no market orders).
    - Before each step, it checks the remaining position qty.
    - Also cancels older exit orders when placing a new one.
    """
    print(f"LADDER EXIT START for {symbol}")

    # start from last trade price seen
    base_price = float(last_price_seen)

    for step in range(6):
        pos = await asyncio.to_thread(get_position, symbol)
        if not pos or float(pos.get("qty", 0)) <= 0:
            print("LADDER EXIT: position already closed")
            return

        qty = float(pos["qty"])
        # Decrease 0.01 each step, but never below original_stop
        price = round(max(original_stop, base_price - 0.01 * step), 2)
        if price <= 0:
            price = 0.01

        # Cancel older exit orders before placing a new one
        await asyncio.to_thread(cancel_open_orders_for_symbol, symbol, "sell")

        print(f"LADDER EXIT step {step+1}: SELL {symbol} qty={qty} @ {price}")
        await asyncio.to_thread(place_limit_order, symbol, qty, "sell", price)

        await asyncio.sleep(5)

        if not await asyncio.to_thread(has_position, symbol):
            print("LADDER EXIT: position closed during ladder")
            return

    # FINAL aggressive limit exit if still in position
    pos = await asyncio.to_thread(get_position, symbol)
    if pos and float(pos.get("qty", 0)) > 0:
        qty = float(pos["qty"])
        # Limit below last seen price to make it marketable
        aggressive_price = round(max(0.01, base_price - 0.05), 2)
        print(f"LADDER EXIT FINAL: SELL {symbol} qty={qty} @ {aggressive_price}")
        await asyncio.to_thread(cancel_open_orders_for_symbol, symbol, "sell")
        await asyncio.to_thread(place_limit_order, symbol, qty, "sell", aggressive_price)
    else:
        print("LADDER EXIT: position was closed before final aggressive order")


# ===================== WEBSOCKET MONITOR (PER-TRADE) =====================

def monitor_trade(trade):
    """
    Runs in its own thread.
    Assumes we already HAVE a filled position and a known entry_price.
    - Uses SIP; if that fails, falls back to IEX.
    - Starts watching for stop / target / trailing.
    """

    symbol = trade["symbol"]
    entry_price = float(trade["entry_price"])
    stop_hard = float(trade["stop"])
    target = float(trade["target"])
    trail_pct = float(trade["trail"]) / 100.0 # 15 -> 0.15

    print(f"MONITOR START for {symbol} entry={entry_price} stop={stop_hard} target={target} trail={trail_pct*100}%")

    # Set up loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run_monitor():
        nonlocal symbol, entry_price, stop_hard, target, trail_pct

        highest = entry_price
        trailing_active = False

        # Try SIP first, fallback to IEX
        stream = None

        async def on_trade(msg):
            nonlocal highest, trailing_active

            price = float(msg.price)
            print(f"PRICE {symbol}: {price}")

            # No position? Stop.
            if not await asyncio.to_thread(has_position, symbol):
                print(f"{symbol}: position gone, stopping monitor")
                await stream.stop()
                return

            if price > highest:
                highest = price

            # Activate trailing after +20% from entry
            if not trailing_active and price >= entry_price * 1.20:
                trailing_active = True
                print(f"{symbol}: trailing activated")

            # Base stop
            stop_now = stop_hard

            if trailing_active:
                trail_stop = round(highest * (1.0 - trail_pct), 2)
                # never below original hard stop
                stop_now = max(stop_hard, trail_stop)

            print(f"{symbol}: high={highest} stop={stop_now} target={target}")

            # Exit conditions
            if price <= stop_now:
                print(f"{symbol}: STOP HIT @ {price}")
                await ladder_exit(symbol, stop_hard, price)
                await log_final_pl(symbol, entry_price, reason="STOP")
                await stream.stop()

            elif price >= target:
                print(f"{symbol}: TARGET HIT @ {price}")
                await ladder_exit(symbol, stop_hard, price)
                await log_final_pl(symbol, entry_price, reason="TARGET")
                await stream.stop()

        async def start_stream(data_feed):
            nonlocal stream
            print(f"Starting stream with feed={data_feed}")
            stream = StockDataStream(API_KEY, SECRET_KEY, feed=data_feed)
            stream.subscribe_trades(on_trade, symbol)
            await stream.run()

        try:
            # Try SIP
            try:
                await start_stream(DataFeed.SIP)
            except Exception as e:
                print(f"SIP stream failed ({e}), falling back to IEX...")
                await start_stream(DataFeed.IEX)

        except Exception as e:
            print(f"MONITOR ERROR for {symbol}: {e}")
        finally:
            # When we get here, monitor is fully done
            with trade_lock:
                global active_trade
                print(f"MONITOR END for {symbol}, clearing active_trade")
                active_trade = None

    loop.run_until_complete(run_monitor())


async def log_final_pl(symbol, entry_price, reason):
    """
    Compute and log P/L using latest filled sell order if available.
    """
    # Give Alpaca a moment to finalize fills
    await asyncio.sleep(1)

    sell_order = await asyncio.to_thread(get_last_filled_order, symbol, "sell")
    if not sell_order:
        print(f"{symbol}: {reason} exit complete, P/L unknown (no sell order found)")
        return

    try:
        exit_price = float(sell_order.get("filled_avg_price", 0.0))
    except (TypeError, ValueError):
        exit_price = 0.0

    side = sell_order.get("side")
    qty = float(sell_order.get("filled_qty", 0.0))

    if qty <= 0 or exit_price <= 0:
        print(f"{symbol}: {reason} exit complete, P/L unknown (bad qty or price)")
        return

    pnl_per_share = exit_price - entry_price
    pnl_total = pnl_per_share * qty
    pnl_pct = (exit_price / entry_price - 1.0) * 100.0

    print(f"{symbol}: EXIT {reason} qty={qty} entry={entry_price} exit={exit_price} "
          f"P/L={pnl_total:.2f} USD ({pnl_pct:.2f}%)")


# ===================== TRADE WORKER =====================

def trade_worker(trade):
    """
    Runs in background thread for a single trade:
    1. Ladder BUY until filled (or abort).
    2. Once filled, start websocket monitor for exits.
    """
    symbol = trade["symbol"]
    qty = trade["qty"]
    entry = trade["entry"]
    stop = trade["stop"]
    target = trade["target"]
    trail = trade["trail"]

    print(f"TRADE START {symbol} qty={qty} entry={entry} stop={stop} target={target} trail={trail}%")

    # 1. Ladder BUY (blocking)
    entry_price, filled_qty = ladder_buy(symbol, qty, entry)

    if not entry_price or filled_qty <= 0:
        print(f"TRADE ABORT {symbol}: buy not filled")
        with trade_lock:
            global active_trade
            active_trade = None
        return

    # Confirm we still have a position
    if not has_position(symbol):
        print(f"TRADE ABORT {symbol}: position vanished after buy")
        with trade_lock:
            active_trade = None
        return

    # Update trade with real entry and qty
    trade["entry_price"] = entry_price
    trade["qty"] = filled_qty

    print(f"TRADE ACTIVE {symbol}: entry_price={entry_price} qty={filled_qty}")
    print(f"{symbol}: monitoring for stop/target/trailing")

    # 2. Start monitor in this same worker thread
    monitor_trade(trade)


# ===================== FLASK ROUTES =====================

@app.route("/", methods=["GET"])
def health():
    return "Bot live", 200


@app.route("/tv", methods=["POST"])
def tv_webhook():
    global active_trade

    try:
        data = request.get_json(force=True)
        print("WEBHOOK RECEIVED:", data)
    except Exception as e:
        return jsonify({"error": "Invalid JSON", "details": str(e)}), 400

    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    try:
        symbol = data["ticker"]
        qty = int(data["quantity"])
        entry = float(data["entry"])
        stop = float(data["stop"])
        target = float(data["target"])
        trail = float(data.get("trail", 15.0))
    except (KeyError, TypeError, ValueError) as e:
        return jsonify({"error": "Missing or invalid fields", "details": str(e)}), 400

    if qty <= 0:
        return jsonify({"error": "Quantity must be > 0"}), 400

    with trade_lock:
        if active_trade is not None:
            return jsonify({"error": f"Another trade is active for {active_trade['symbol']}"}), 429

        # Set global state for this trade
        active_trade = {
            "symbol": symbol,
            "qty": qty,
            "entry": entry,
            "stop": stop,
            "target": target,
            "trail": trail
        }

        t = threading.Thread(target=trade_worker, args=(active_trade,), daemon=True)
        t.start()

    return jsonify({"msg": f"Trade started for {symbol}"}), 200


if __name__ == "__main__":
    print("Starting bot...")
    app.run(host="0.0.0.0", port=8080)



























































































































