import json
import logging
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Flask, request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    ReplaceOrderRequest,
    GetOrdersRequest,
    StopLimitOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, OrderType, OrderClass

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("trading_bot")

app = Flask(__name__)

is_live = os.getenv("LIVE_TRADING", "False") == "True"
logger.info(f"--- BOT STARTED: LIVE_TRADING={is_live} ---")
trading_client = TradingClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"), paper=not is_live)

HWM_FILE = "hwm_data.json"
manager_status = {"last_heartbeat": time.time(), "is_alive": True}
symbol_error_counts = {}
symbol_alert_cooldown = {}
# Tracks symbols that have already had their stop moved to BE
be_moved = set() 
ERROR_ALERT_INTERVAL = 10

MAX_POSITION_SIZE = int(os.getenv("MAX_POSITION_SIZE", "500"))
TRADING_WINDOW_START = os.getenv("TRADING_WINDOW_START", "09:45")
TRADING_WINDOW_END = os.getenv("TRADING_WINDOW_END", "20:00")
NY_TZ = ZoneInfo("America/New_York")

order_lock = threading.Lock()
recent_signals = {}
SIGNAL_DEDUPE_WINDOW_SECONDS = 5

HWM_SAVE_INTERVAL_SECONDS = 5
_last_hwm_save = 0.0

def load_hwm():
    if os.path.exists(HWM_FILE):
        try:
            with open(HWM_FILE, "r") as f: return json.load(f)
        except Exception: return {}
    return {}

def save_hwm(hwm):
    with open(HWM_FILE, "w") as f: json.dump(hwm, f)

def maybe_save_hwm(hwm, force=False):
    global _last_hwm_save
    now = time.time()
    if force or (now - _last_hwm_save) >= HWM_SAVE_INTERVAL_SECONDS:
        save_hwm(hwm)
        _last_hwm_save = now

def send_alert(message):
    logger.info(f"ALERT: {message}")

def emergency_flatten():
    send_alert("CRITICAL: Emergency Flatten Triggered!")
    try:
        trading_client.close_all_positions(cancel_orders=True)
    except Exception as e:
        logger.error(f"Flattening failed: {e}")
    finally:
        manager_status["is_alive"] = False

def handle_symbol_error(symbol, e):
    symbol_error_counts[symbol] = symbol_error_counts.get(symbol, 0) + 1
    count = symbol_error_counts[symbol]
    logger.error(f"Error managing {symbol}: {e}")
    if count == 3:
        send_alert(f"CRITICAL: Symbol {symbol} failing repeatedly ({count} consecutive errors).")
        symbol_alert_cooldown[symbol] = count
    elif count > 3 and (count - symbol_alert_cooldown.get(symbol, 3)) >= ERROR_ALERT_INTERVAL:
        send_alert(f"CRITICAL: Symbol {symbol} still failing ({count} consecutive errors).")
        symbol_alert_cooldown[symbol] = count

def within_trading_window():
    now_ny = datetime.now(NY_TZ).time()
    start = datetime.strptime(TRADING_WINDOW_START, "%H:%M").time()
    end = datetime.strptime(TRADING_WINDOW_END, "%H:%M").time()
    return start <= now_ny <= end

def has_open_exposure(symbol):
    positions = trading_client.get_all_positions()
    if any(p.symbol == symbol for p in positions): return True
    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=OrderStatus.OPEN))
    if any(o.symbol == symbol and o.side == OrderSide.BUY for o in open_orders): return True
    return False

def position_manager():
    hwm = load_hwm()
    while True:
        try:
            manager_status["last_heartbeat"] = time.time()
            manager_status["is_alive"] = True
            
            positions = trading_client.get_all_positions()
            # Prune be_moved if the position no longer exists (TP/SL/Manual close)
            active_symbols = {p.symbol for p in positions}
            be_moved.intersection_update(active_symbols)
            
            if positions:
                open_orders = {
                    o.symbol: o for o in trading_client.get_orders(filter=GetOrdersRequest(status=OrderStatus.OPEN)) 
                    if o.type == OrderType.STOP
                }
                for pos in positions:
                    symbol = pos.symbol
                    try:
                        current, entry = float(pos.current_price), float(pos.avg_entry_price)
                        if symbol not in hwm or current > hwm[symbol]:
                            hwm[symbol] = current
                            maybe_save_hwm(hwm)
                        
                        # Move to Breakeven (Exactly Once)
                        if current >= (entry * 1.02) and symbol in open_orders and symbol not in be_moved:
                            order = open_orders[symbol]
                            trading_client.replace_order_by_id(order.id, ReplaceOrderRequest(stop_price=entry))
                            be_moved.add(symbol)
                            logger.info(f"MOVED TO BE: {symbol}")
                            
                        # 10% Trailing Stop -> Market Order Exit
                        if current >= (entry * 1.02) and current <= (hwm[symbol] * 0.90):
                            logger.info(f"TRAILING HIT: Exiting {symbol}")
                            if symbol in open_orders: trading_client.cancel_order_by_id(open_orders[symbol].id)
                            trading_client.close_position(symbol)
                            if symbol in hwm:
                                del hwm[symbol]
                                maybe_save_hwm(hwm, force=True)
                            send_alert(f"Position {symbol} closed via Trailing Stop.")
                            symbol_error_counts[symbol] = 0
                            symbol_alert_cooldown.pop(symbol, None)
                    except Exception as e:
                        handle_symbol_error(symbol, e)
        except Exception as e:
            if any(code in str(e) for code in ["401", "403"]):
                emergency_flatten()
                break
            logger.warning(f"Transient error: {e}. Retrying...")
        
        time.sleep(2)

threading.Thread(target=position_manager, daemon=True).start()

@app.route("/health", methods=["GET"])
def health():
    return "OK", 200

@app.route("/", methods=["POST"])
def webhook():
    global recent_signals
    if not manager_status["is_alive"] or (time.time() - manager_status["last_heartbeat"] > 60):
        send_alert("REJECTED SIGNAL: Manager thread offline.")
        return "System Offline", 503
    data = request.get_json(force=True, silent=True)
    if not data: return "Bad Request", 400
    if data.get("secret") != os.getenv("WEBHOOK_SECRET"): return "Unauthorized", 401
    try:
        symbol = str(data["symbol"]).upper()
        qty, buy_stop = int(data["qty"]), float(data["buy_stop"])
        buy_limit, take_profit = float(data["buy_limit"]), float(data["take_profit"])
        stop_loss = float(data["stop_loss"])
    except (KeyError, ValueError, TypeError): return "Bad Request", 400
    if not (stop_loss < buy_stop <= buy_limit < take_profit): return "Bad Request: prices", 400
    if not within_trading_window(): return "Outside trading hours", 200
    if qty > MAX_POSITION_SIZE: return "Qty exceeds max", 200
    
    with order_lock:
        now = time.time()
        recent_signals = {s: t for s, t in recent_signals.items() if now - t < SIGNAL_DEDUPE_WINDOW_SECONDS}
        if recent_signals.get(symbol) is not None:
            logger.info(f"Duplicate signal ignored for {symbol}")
            return "Duplicate", 200
        try:
            if has_open_exposure(symbol):
                logger.info(f"Duplicate signal ignored for {symbol}")
                return "Duplicate", 200
            
            account = trading_client.get_account()
            if float(account.buying_power) < (qty * buy_limit):
                send_alert(f"REJECTED: Insufficient buying power for {symbol}")
                return "Insufficient buying power", 200

            order = StopLimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                          stop_price=buy_stop, limit_price=buy_limit, order_class=OrderClass.BRACKET,
                                          take_profit=TakeProfitRequest(limit_price=take_profit),
                                          stop_loss=StopLossRequest(stop_price=stop_loss))
            trading_client.submit_order(order)
            recent_signals[symbol] = now
            send_alert(f"Entry placed: {symbol} qty={qty}")
        except Exception as e:
            send_alert(f"CRITICAL: Failed to submit entry for {symbol}: {e}")
            return "Order failed", 500
    return "Success", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))



























































































































