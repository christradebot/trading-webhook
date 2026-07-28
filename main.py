import json
import logging
import os
import random
import signal
import sys
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional, Set, TypeVar
from zoneinfo import ZoneInfo

from flask import Flask, request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    ReplaceOrderRequest,
    GetOrdersRequest,
    GetCalendarRequest,
    StopLimitOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderClass, QueryOrderStatus, OrderStatus

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ============================================================
# Logging Configuration
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("trading_bot")

app = Flask(__name__)

is_live: bool = os.getenv("LIVE_TRADING", "False") == "True"
logger.info(f"--- BOT STARTED: LIVE_TRADING={is_live} ---")

# ============================================================
# Environment / Config Constants
# ============================================================
API_TIMEOUT: float = float(os.getenv("API_TIMEOUT", "15"))
RETRY_ATTEMPTS: int = int(os.getenv("RETRY_ATTEMPTS", "3"))
RETRY_BASE_DELAY: float = float(os.getenv("RETRY_BASE_DELAY", "0.5"))
MANAGER_LOOP_INTERVAL: float = float(os.getenv("MANAGER_LOOP_INTERVAL", "0.5"))
HWM_SAVE_INTERVAL_SECONDS: int = int(os.getenv("HWM_SAVE_INTERVAL_SECONDS", "1"))

TRADING_WINDOW_START: str = os.getenv("TRADING_WINDOW_START", "09:40")
TRADING_WINDOW_END: str = os.getenv("TRADING_WINDOW_END", "16:00")
NY_TZ = ZoneInfo("America/New_York")

HWM_FILE: str = "hwm_data.json"
manager_status: dict[str, Any] = {"last_heartbeat": time.time(), "is_alive": True}
symbol_error_counts: dict[str, int] = {}
symbol_alert_cooldown: dict[str, int] = {}
be_moved: set[str] = set()
quarantined_symbols: set[str] = set()

ERROR_ALERT_INTERVAL: int = 10
QUARANTINE_THRESHOLD: int = 5

BREAKEVEN_TRIGGER_PCT: float = 0.02
TRAIL_GIVEBACK_PCT: float = 0.90

ORB_SPREAD_MAX: float = float(os.getenv("ORB_SPREAD_MAX", "0.02"))
ORB_STOP_LOSS_PCT_DEFAULT: float = float(os.getenv("ORB_STOP_LOSS_PCT", "0.05"))
ORB_COOLDOWN_MINUTES: float = float(os.getenv("ORB_COOLDOWN_MINUTES", "60"))
ORB_WEBHOOK_SECRET: str = os.getenv("ORB_WEBHOOK_SECRET", os.getenv("WEBHOOK_SECRET", ""))

orb_cooldown_until: dict[str, float] = {}
orb_pending_stop: dict[str, dict[str, float]] = {}

# ============================================================
# Response String Constants
# ============================================================
RESP_SUCCESS: str = "Success"
RESP_BAD_REQUEST: str = "Bad Request"
RESP_UNAUTHORIZED: str = "Unauthorized"
RESP_OFFLINE: str = "System Offline"
RESP_QUARANTINED: str = "Symbol quarantined"
RESP_NOT_TRADING_DAY: str = "Not a trading day"
RESP_OUTSIDE_HOURS: str = "Outside trading hours"
RESP_DUPLICATE: str = "Duplicate"
RESP_POSITION_OPEN: str = "Another position already open"
RESP_INSUFFICIENT_BP: str = "Insufficient buying power"
RESP_ORDER_FAILED: str = "Order failed"
RESP_SPREAD_WIDE: str = "Spread too wide"
RESP_COOLDOWN: str = "Cooldown active"

# ============================================================
# Alpaca Client Initialization with Native Timeouts
# ============================================================
try:
    trading_client = TradingClient(
        os.getenv("APCA_API_KEY_ID"),
        os.getenv("APCA_API_SECRET_KEY"),
        paper=not is_live,
        timeout=API_TIMEOUT
    )
    data_client = StockHistoricalDataClient(
        os.getenv("APCA_API_KEY_ID"),
        os.getenv("APCA_API_SECRET_KEY"),
        timeout=API_TIMEOUT
    )
except Exception:
    logger.exception("CRITICAL: Failed to initialize Alpaca clients.")
    raise SystemExit(1)

# Startup Account Verification & Abort on Failure
try:
    account_info = trading_client.get_account()
    logger.info(f"Account Status: {account_info.status}")
    logger.info(f"Buying Power: {account_info.buying_power}")
    logger.info(f"Trading Blocked: {account_info.trading_blocked}")
    if account_info.trading_blocked:
        logger.error("CRITICAL: Alpaca account is currently trading blocked!")
        raise SystemExit(1)
except Exception:
    logger.exception("CRITICAL: Failed to verify account status on startup.")
    raise SystemExit(1)

order_lock = threading.Lock()
recent_signals: dict[str, float] = {}
orb_recent_signals: dict[str, float] = {}
SIGNAL_DEDUPE_WINDOW_SECONDS: float = 5.0

_last_hwm_save: float = 0.0


def calculate_qty(buy_limit: float, buying_power: float) -> int:
    """Calculates integer share quantity leaving a 2% safety buffer for price fluctuations."""
    usable = buying_power * 0.98
    return int(usable // buy_limit)


def any_position_open() -> bool:
    """Checks if any positions or open buy orders currently exist."""
    positions = alpaca_retry(trading_client.get_all_positions)
    if positions:
        return True
    open_orders = alpaca_retry(trading_client.get_orders, filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    if any(o.side == OrderSide.BUY for o in open_orders):
        return True
    return False


def load_hwm() -> dict[str, float]:
    """Loads high-water mark records from persistent storage."""
    if os.path.exists(HWM_FILE):
        try:
            with open(HWM_FILE, "r") as f:
                return json.load(f)
        except Exception:
            logger.exception("Failed to load HWM file.")
            return {}
    return {}


def save_hwm(hwm: dict[str, float]) -> None:
    """Saves high-water mark records to persistent storage."""
    with open(HWM_FILE, "w") as f:
        json.dump(hwm, f)


def maybe_save_hwm(hwm: dict[str, float], force: bool = False) -> None:
    """Conditionally saves HWM data based on elapsed time or forced updates."""
    global _last_hwm_save
    now = time.time()
    if force or (now - _last_hwm_save) >= HWM_SAVE_INTERVAL_SECONDS:
        save_hwm(hwm)
        _last_hwm_save = now


def round_to_tick(price: float) -> float:
    """Rounds a given price to standard US equity tick increments."""
    tick = 0.01 if price >= 1.0 else 0.0001
    return round(round(price / tick) * tick, 4)


def send_alert(message: str) -> None:
    """Dispatches operational alert messages to logging streams."""
    logger.info(f"ALERT: {message}")


T = TypeVar("T")

# ============================================================
# Robust Retry Wrapper with Exponential Backoff and Jitter
# ============================================================
def alpaca_retry(func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Executes Alpaca API calls with exponential backoff and jitter using native client timeouts."""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if attempt == RETRY_ATTEMPTS - 1:
                raise e
            delay = RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, 0.25)
            logger.warning(f"Alpaca API call failed ({e}). Retrying in {delay:.2f}s... (Attempt {attempt + 1}/{RETRY_ATTEMPTS})")
            time.sleep(delay)
    raise RuntimeError("Unreachable retry state")


def emergency_flatten() -> None:
    """Flattens all open positions and cancels open orders during a critical failure."""
    send_alert("CRITICAL: Emergency Flatten Triggered!")
    try:
        alpaca_retry(trading_client.close_all_positions, cancel_orders=True)
    except Exception:
        logger.exception("Flattening failed during emergency shutdown.")
    finally:
        manager_status["is_alive"] = False


# ============================================================
# Graceful Shutdown Handler (SIGTERM / SIGINT)
# ============================================================
def handle_shutdown(signum: int, frame: Any) -> None:
    """Handles termination signals for clean container shutdowns."""
    logger.info(f"Received signal {signum}. Shutting down gracefully...")
    manager_status["is_alive"] = False
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_shutdown)
signal.signal(signal.SIGINT, handle_shutdown)


def is_auth_error(e: Exception) -> bool:
    """Determines if an exception is related to authentication failures."""
    status_code = getattr(e, "status_code", None)
    if status_code in (401, 403):
        return True
    return any(code in str(e) for code in ["401", "403"])


def handle_symbol_error(symbol: str, e: Exception) -> None:
    """Tracks consecutive errors per symbol and quarantines if necessary."""
    symbol_error_counts[symbol] = symbol_error_counts.get(symbol, 0) + 1
    count = symbol_error_counts[symbol]
    logger.exception(f"Error managing {symbol}: {e}")

    if count == 3:
        send_alert(f"WARNING: Symbol {symbol} failing repeatedly ({count} consecutive errors).")
        symbol_alert_cooldown[symbol] = count
    elif 3 < count < QUARANTINE_THRESHOLD and (count - symbol_alert_cooldown.get(symbol, 3)) >= ERROR_ALERT_INTERVAL:
        send_alert(f"WARNING: Symbol {symbol} still failing ({count} consecutive errors).")
        symbol_alert_cooldown[symbol] = count

    if count >= QUARANTINE_THRESHOLD and symbol not in quarantined_symbols:
        quarantined_symbols.add(symbol)
        send_alert(
            f"CRITICAL: Symbol {symbol} quarantined after {count} consecutive errors. "
            f"Automated management STOPPED for this symbol. Manual intervention required."
        )
    elif count > QUARANTINE_THRESHOLD and (count - symbol_alert_cooldown.get(symbol, QUARANTINE_THRESHOLD)) >= ERROR_ALERT_INTERVAL:
        send_alert(f"CRITICAL: Symbol {symbol} still quarantined and failing ({count} consecutive errors).")
        symbol_alert_cooldown[symbol] = count


def within_trading_window() -> bool:
    """Checks if current Eastern Time falls within the configured trading window."""
    now_ny = datetime.now(NY_TZ).time()
    start = datetime.strptime(TRADING_WINDOW_START, "%H:%M").time()
    end = datetime.strptime(TRADING_WINDOW_END, "%H:%M").time()
    return start <= now_ny <= end


def is_trading_day() -> bool:
    """Verifies with Alpaca calendar if today is an active trading session."""
    try:
        today_ny = datetime.now(NY_TZ).date()
        calendar_days = alpaca_retry(
            trading_client.get_calendar,
            filters=GetCalendarRequest(start=today_ny, end=today_ny)
        )
        return len(calendar_days) > 0
    except Exception:
        logger.exception("Failed to verify trading day, failing safe (rejecting signal).")
        return False


def safe_close_position(symbol: str) -> None:
    """Closes a specific position and cancels any leftover bracket orders."""
    alpaca_retry(trading_client.close_position, symbol)
    try:
        leftover_orders = alpaca_retry(
            trading_client.get_orders,
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )
        for o in leftover_orders:
            if o.symbol == symbol:
                alpaca_retry(trading_client.cancel_order_by_id, o.id)
                logger.warning(f"Cancelled leftover order {o.id} for {symbol} after close_position.")
    except Exception:
        logger.exception(f"Failed cleanup check for leftover orders on {symbol}.")


def start_orb_cooldown(symbol: str) -> None:
    """Initiates an ORB trading cooldown period for a given symbol."""
    orb_cooldown_until[symbol] = time.time() + ORB_COOLDOWN_MINUTES * 60
    logger.info(f"ORB cooldown started for {symbol}: blocked until {ORB_COOLDOWN_MINUTES} min from now")


def orb_in_cooldown(symbol: str) -> bool:
    """Checks whether an ORB symbol is currently under cooldown restriction."""
    until = orb_cooldown_until.get(symbol)
    return until is not None and time.time() < until


def log_untracked_closure(symbol: str) -> None:
    """Logs details when a position closes externally (e.g., via standalone stop-loss or take-profit)."""
    try:
        closed_orders = alpaca_retry(
            trading_client.get_orders,
            filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=10)
        )
        filled = [o for o in closed_orders if o.filled_qty and float(o.filled_qty) > 0]
        if filled:
            o = filled[0]
            send_alert(
                f"Position {symbol} closed (stop-loss or take-profit filled): "
                f"{o.side.value} {o.filled_qty}@{o.filled_avg_price} ({o.type.value})"
            )
        else:
            send_alert(f"Position {symbol} closed - no recent fill details found on lookup.")
    except Exception:
        logger.exception(f"Failed to look up closure detail for {symbol}.")
        send_alert(f"Position {symbol} closed - could not fetch fill details.")
    finally:
        start_orb_cooldown(symbol)


def submit_orb_stop_if_needed(symbol: str, avg_entry_price: float, open_orders: dict[str, Any]) -> None:
    """Submits a trailing stop leg for ORB orders once filled."""
    with order_lock:
        pending = orb_pending_stop.get(symbol)
        if not pending:
            return
        if symbol in open_orders:
            orb_pending_stop.pop(symbol, None)
            return
    try:
        stop_pct = pending["stop_loss_pct"]
        stop_price = round_to_tick(avg_entry_price * (1 - stop_pct))
        position = alpaca_retry(trading_client.get_open_position, symbol)
        qty = abs(int(float(position.qty)))
        order = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            stop_price=stop_price,
        )
        alpaca_retry(trading_client.submit_order, order)
        send_alert(f"ORB stop-loss submitted for {symbol}: entry={avg_entry_price} stop={stop_price}")
        with order_lock:
            orb_pending_stop.pop(symbol, None)
    except Exception:
        logger.exception(f"Failed to submit ORB stop-loss for {symbol}.")


def position_manager() -> None:
    """Background worker thread managing high-water marks, breakeven adjustments, and trailing stops."""
    hwm = load_hwm()
    previous_active_symbols: set[str] = set()
    bot_initiated_closes: set[str] = set()
    while manager_status["is_alive"]:
        loop_start = time.time()
        try:
            manager_status["last_heartbeat"] = time.time()

            positions = alpaca_retry(trading_client.get_all_positions)
            active_symbols = {p.symbol for p in positions}

            externally_closed = previous_active_symbols - active_symbols - bot_initiated_closes
            for sym in externally_closed:
                log_untracked_closure(sym)
            bot_initiated_closes.clear()
            previous_active_symbols = active_symbols

            be_moved.intersection_update(active_symbols)
            quarantined_symbols.intersection_update(active_symbols)

            if positions:
                orders_list = alpaca_retry(
                    trading_client.get_orders,
                    filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
                )
                open_orders = {o.symbol: o for o in orders_list if o.type == OrderType.STOP}
                
                for pos in positions:
                    symbol = pos.symbol

                    if symbol in quarantined_symbols:
                        continue

                    try:
                        current, entry = float(pos.current_price), float(pos.avg_entry_price)

                        if symbol in orb_pending_stop:
                            submit_orb_stop_if_needed(symbol, entry, open_orders)

                        if symbol not in hwm or current > hwm[symbol]:
                            hwm[symbol] = current
                            maybe_save_hwm(hwm, force=True)

                        if current >= (entry * (1 + BREAKEVEN_TRIGGER_PCT)) and symbol in open_orders and symbol not in be_moved:
                            order = open_orders[symbol]
                            alpaca_retry(
                                trading_client.replace_order_by_id,
                                order.id,
                                ReplaceOrderRequest(stop_price=entry)
                            )
                            be_moved.add(symbol)
                            logger.info(f"MOVED TO BE: {symbol}")

                        if symbol in be_moved and current <= (hwm[symbol] * TRAIL_GIVEBACK_PCT):
                            logger.info(f"TRAILING HIT: Exiting {symbol}")
                            if symbol in open_orders:
                                alpaca_retry(trading_client.cancel_order_by_id, open_orders[symbol].id)
                            safe_close_position(symbol)
                            bot_initiated_closes.add(symbol)
                            start_orb_cooldown(symbol)
                            if symbol in hwm:
                                del hwm[symbol]
                                maybe_save_hwm(hwm, force=True)
                            send_alert(f"Position {symbol} closed via Trailing Stop.")
                            symbol_error_counts[symbol] = 0
                            symbol_alert_cooldown.pop(symbol, None)
                    except Exception as e:
                        handle_symbol_error(symbol, e)
        except Exception as e:
            if is_auth_error(e):
                emergency_flatten()
                break
            logger.warning(f"Transient error in position manager: {e}. Retrying...")

        loop_duration = time.time() - loop_start
        if loop_duration > 1.0:
            logger.warning(f"Position manager loop took high duration: {loop_duration:.3f}s")

        time.sleep(MANAGER_LOOP_INTERVAL)


threading.Thread(target=position_manager, daemon=True).start()


@app.route("/health", methods=["GET"])
def health() -> tuple[str, int]:
    """Health check endpoint for container orchestration."""
    return "OK", 200


@app.route("/", methods=["POST"])
def webhook() -> tuple[str, int]:
    """Standard PMH breakout webhook endpoint."""
    global recent_signals
    if not manager_status["is_alive"] or (time.time() - manager_status["last_heartbeat"] > 60):
        send_alert("REJECTED SIGNAL: Manager thread offline.")
        return RESP_OFFLINE, 503
    data = request.get_json(force=True, silent=True)
    if not data:
        return RESP_BAD_REQUEST, 400
    if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
        return RESP_UNAUTHORIZED, 401
    try:
        symbol = str(data["symbol"]).upper()
        buy_stop = round_to_tick(float(data["buy_stop"]))
        buy_limit, take_profit = round_to_tick(float(data["buy_limit"])), round_to_tick(float(data["take_profit"]))
        stop_loss = round_to_tick(float(data["stop_loss"]))
    except (KeyError, ValueError, TypeError):
        return RESP_BAD_REQUEST, 400
    if not (stop_loss < buy_stop <= buy_limit < take_profit):
        return f"{RESP_BAD_REQUEST}: prices", 400
    if symbol in quarantined_symbols:
        send_alert(f"REJECTED SIGNAL: {symbol} is quarantined pending manual review.")
        return RESP_QUARANTINED, 200
    if not is_trading_day():
        return RESP_NOT_TRADING_DAY, 200
    if not within_trading_window():
        return RESP_OUTSIDE_HOURS, 200

    with order_lock:
        now = time.time()
        recent_signals = {s: t for s, t in recent_signals.items() if now - t < SIGNAL_DEDUPE_WINDOW_SECONDS}
        if recent_signals.get(symbol) is not None:
            logger.info(f"Duplicate signal ignored for {symbol}")
            return RESP_DUPLICATE, 200
        try:
            if any_position_open():
                logger.info(f"Signal ignored for {symbol}: another position/order is already open")
                return RESP_POSITION_OPEN, 200

            account = alpaca_retry(trading_client.get_account)
            qty = calculate_qty(buy_limit, float(account.buying_power))
            if qty < 1:
                send_alert(f"REJECTED: Insufficient buying power for {symbol} at {buy_limit}")
                return RESP_INSUFFICIENT_BP, 200

            order = StopLimitOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                stop_price=buy_stop, limit_price=buy_limit, order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=take_profit),
                stop_loss=StopLossRequest(stop_price=stop_loss)
            )
            alpaca_retry(trading_client.submit_order, order)
            recent_signals[symbol] = now
            send_alert(
                f"Entry placed: {symbol} qty={qty} buy_stop={buy_stop} buy_limit={buy_limit} "
                f"stop_loss={stop_loss} take_profit={take_profit}"
            )
        except Exception:
            logger.exception(f"CRITICAL: Failed to submit entry for {symbol}.")
            return RESP_ORDER_FAILED, 500
    return RESP_SUCCESS, 200


def cancel_if_unfilled(order_id: str, symbol: str) -> None:
    """Cancels an unfilled ORB limit entry after the timeout window elapses."""
    try:
        order = alpaca_retry(trading_client.get_order_by_id, order_id)
        if order.status not in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            alpaca_retry(trading_client.cancel_order_by_id, order_id)
            logger.info(f"Cancelled unfilled ORB entry for {symbol} after timeout")
            send_alert(f"ORB entry for {symbol} cancelled — unfilled after timeout window")
            with order_lock:
                if not order.filled_qty or float(order.filled_qty) == 0:
                    orb_pending_stop.pop(symbol, None)
    except Exception:
        logger.exception(f"Error checking/cancelling ORB entry for {symbol}.")


def get_current_spread(symbol: str) -> float:
    """Fetches the latest bid-ask spread for a given symbol with retry protection."""
    def fetch_quote() -> Any:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        return data_client.get_stock_latest_quote(req)[symbol]

    try:
        quote = alpaca_retry(fetch_quote)
        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)
        if bid <= 0 or ask <= 0:
            return float("inf")
        return ask - bid
    except Exception:
        logger.exception(f"Failed fetching latest quote spread for {symbol}.")
        return float("inf")


@app.route("/orb", methods=["POST"])
def orb_webhook() -> tuple[str, int]:
    """Opening Range Breakout (ORB) webhook endpoint."""
    global orb_recent_signals
    if not manager_status["is_alive"] or (time.time() - manager_status["last_heartbeat"] > 60):
        send_alert("REJECTED ORB SIGNAL: Manager thread offline.")
        return RESP_OFFLINE, 503

    data = request.get_json(force=True, silent=True)
    if not data:
        return RESP_BAD_REQUEST, 400
    if data.get("secret") != ORB_WEBHOOK_SECRET:
        return RESP_UNAUTHORIZED, 401
    try:
        symbol = str(data["ticker"]).upper()
        action = str(data["action"]).upper()
        limit_price = round_to_tick(float(data["limit_price"]))
        stop_loss_pct = float(data.get("stop_loss_pct", ORB_STOP_LOSS_PCT_DEFAULT * 100)) / 100
        cancel_after_sec = float(data.get("cancel_after_sec", 10))
    except (KeyError, ValueError, TypeError):
        return RESP_BAD_REQUEST, 400

    if action != "BUY":
        return "Ignored (non-BUY action)", 200
    if symbol in quarantined_symbols:
        send_alert(f"REJECTED ORB SIGNAL: {symbol} is quarantined pending manual review.")
        return RESP_QUARANTINED, 200
    if not is_trading_day():
        return RESP_NOT_TRADING_DAY, 200
    if not within_trading_window():
        return RESP_OUTSIDE_HOURS, 200
    if orb_in_cooldown(symbol):
        logger.info(f"ORB signal for {symbol} ignored: still in cooldown")
        return RESP_COOLDOWN, 200

    with order_lock:
        now = time.time()
        orb_recent_signals = {s: t for s, t in orb_recent_signals.items() if now - t < SIGNAL_DEDUPE_WINDOW_SECONDS}
        if orb_recent_signals.get(symbol) is not None:
            logger.info(f"Duplicate ORB signal ignored for {symbol}")
            return RESP_DUPLICATE, 200
        try:
            if any_position_open():
                logger.info(f"ORB signal ignored for {symbol}: another position/order is already open")
                return RESP_POSITION_OPEN, 200

            spread = get_current_spread(symbol)
            if spread > ORB_SPREAD_MAX:
                send_alert(f"REJECTED ORB SIGNAL: {symbol} spread ${spread:.4f} exceeds ${ORB_SPREAD_MAX:.2f} cap")
                return RESP_SPREAD_WIDE, 200

            account = alpaca_retry(trading_client.get_account)
            qty = calculate_qty(limit_price, float(account.buying_power))
            if qty < 1:
                send_alert(f"REJECTED: Insufficient buying power for {symbol} at {limit_price}")
                return RESP_INSUFFICIENT_BP, 200

            order = LimitOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
            )
            submitted = alpaca_retry(trading_client.submit_order, order)
            orb_recent_signals[symbol] = now
            orb_pending_stop[symbol] = {"stop_loss_pct": stop_loss_pct}

            threading.Timer(cancel_after_sec, cancel_if_unfilled, args=[submitted.id, symbol]).start()

            send_alert(
                f"ORB entry placed: {symbol} qty={qty} limit={limit_price} "
                f"stop_pct={stop_loss_pct*100:.1f}% cancel_after={cancel_after_sec}s"
            )
        except Exception:
            logger.exception(f"CRITICAL: Failed to submit ORB entry for {symbol}.")
            return RESP_ORDER_FAILED, 500
    return RESP_SUCCESS, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), threaded=True)


























































































































