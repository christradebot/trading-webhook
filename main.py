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
    GetCalendarRequest,
    StopLimitOrderRequest,
    LimitOrderRequest,
    StopOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderClass, QueryOrderStatus, OrderStatus

# Level-1 quote client for the ORB spread check.
# (Package path can vary slightly by alpaca-py version — adjust if your
# install differs, e.g. `from alpaca.data.historical.stock import StockHistoricalDataClient`)
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

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
data_client = StockHistoricalDataClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"))

HWM_FILE = "hwm_data.json"
manager_status = {"last_heartbeat": time.time(), "is_alive": True}
symbol_error_counts = {}
symbol_alert_cooldown = {}
be_moved = set()
quarantined_symbols = set()

ERROR_ALERT_INTERVAL = 10
QUARANTINE_THRESHOLD = 5

# Already matches the ORB design (2% breakeven arm, 10% trailing giveback) —
# reused as-is for ORB positions, no separate constants needed.
BREAKEVEN_TRIGGER_PCT = 0.02
TRAIL_GIVEBACK_PCT = 0.90

# ============================================================
# ORB-specific config
# ============================================================
ORB_SPREAD_MAX = float(os.getenv("ORB_SPREAD_MAX", "0.02"))          # flat $0.02 cap, confirmed
ORB_STOP_LOSS_PCT_DEFAULT = float(os.getenv("ORB_STOP_LOSS_PCT", "0.05"))
ORB_COOLDOWN_MINUTES = float(os.getenv("ORB_COOLDOWN_MINUTES", "60"))  # placeholder — you were between 60-90, tune via env var
ORB_WEBHOOK_SECRET = os.getenv("ORB_WEBHOOK_SECRET", os.getenv("WEBHOOK_SECRET"))

# symbol -> unix timestamp until which new ORB entries are blocked
orb_cooldown_until = {}
# symbol -> {"stop_loss_pct": float} for positions still waiting on their
# real stop-loss order to be submitted once the fill price is known
orb_pending_stop = {}


def calculate_qty(buy_limit, buying_power):
    usable = buying_power * 0.98
    return int(usable // buy_limit)


def any_position_open():
    positions = trading_client.get_all_positions()
    if positions:
        return True
    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    if any(o.side == OrderSide.BUY for o in open_orders):
        return True
    return False


TRADING_WINDOW_START = os.getenv("TRADING_WINDOW_START", "09:40")
TRADING_WINDOW_END = os.getenv("TRADING_WINDOW_END", "16:00")
NY_TZ = ZoneInfo("America/New_York")

order_lock = threading.Lock()
recent_signals = {}
SIGNAL_DEDUPE_WINDOW_SECONDS = 5

HWM_SAVE_INTERVAL_SECONDS = 5
_last_hwm_save = 0.0


def load_hwm():
    if os.path.exists(HWM_FILE):
        try:
            with open(HWM_FILE, "r") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_hwm(hwm):
    with open(HWM_FILE, "w") as f:
        json.dump(hwm, f)


def maybe_save_hwm(hwm, force=False):
    global _last_hwm_save
    now = time.time()
    if force or (now - _last_hwm_save) >= HWM_SAVE_INTERVAL_SECONDS:
        save_hwm(hwm)
        _last_hwm_save = now


def round_to_tick(price):
    tick = 0.01 if price >= 1.0 else 0.0001
    return round(round(price / tick) * tick, 4)


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


def is_auth_error(e):
    status_code = getattr(e, "status_code", None)
    if status_code in (401, 403):
        return True
    return any(code in str(e) for code in ["401", "403"])


def handle_symbol_error(symbol, e):
    symbol_error_counts[symbol] = symbol_error_counts.get(symbol, 0) + 1
    count = symbol_error_counts[symbol]
    logger.error(f"Error managing {symbol}: {e}")

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


def within_trading_window():
    now_ny = datetime.now(NY_TZ).time()
    start = datetime.strptime(TRADING_WINDOW_START, "%H:%M").time()
    end = datetime.strptime(TRADING_WINDOW_END, "%H:%M").time()
    return start <= now_ny <= end


def is_trading_day():
    try:
        today_ny = datetime.now(NY_TZ).date()
        calendar_days = trading_client.get_calendar(
            filters=GetCalendarRequest(start=today_ny, end=today_ny)
        )
        return len(calendar_days) > 0
    except Exception as e:
        logger.error(f"Failed to verify trading day, failing safe (rejecting signal): {e}")
        return False


def safe_close_position(symbol):
    trading_client.close_position(symbol)
    try:
        leftover_orders = trading_client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.OPEN)
        )
        for o in leftover_orders:
            if o.symbol == symbol:
                trading_client.cancel_order_by_id(o.id)
                logger.warning(f"Cancelled leftover order {o.id} for {symbol} after close_position.")
    except Exception as e:
        logger.error(f"Failed cleanup check for leftover orders on {symbol}: {e}")


def start_orb_cooldown(symbol):
    """Called whenever an ORB position exits via stop-loss or trailing —
    blocks re-entry on this symbol for ORB_COOLDOWN_MINUTES."""
    orb_cooldown_until[symbol] = time.time() + ORB_COOLDOWN_MINUTES * 60
    logger.info(f"ORB cooldown started for {symbol}: blocked until {ORB_COOLDOWN_MINUTES} min from now")


def orb_in_cooldown(symbol):
    until = orb_cooldown_until.get(symbol)
    return until is not None and time.time() < until


def log_untracked_closure(symbol):
    """A position vanished without the bot having closed it itself — i.e.
    the broker-side stop order (or breakeven-moved stop) filled. This is
    the 'stopped out' event for ORB cooldown purposes."""
    try:
        closed_orders = trading_client.get_orders(
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
    except Exception as e:
        logger.error(f"Failed to look up closure detail for {symbol}: {e}")
        send_alert(f"Position {symbol} closed - could not fetch fill details ({e}).")
    finally:
        # Any stop-driven exit (PMH or ORB) starts the ORB cooldown for this
        # symbol. Harmless no-op for symbols that were never an ORB trade.
        start_orb_cooldown(symbol)


def submit_orb_stop_if_needed(symbol, avg_entry_price, open_orders):
    """For ORB positions: the entry went in as a plain limit order (no
    bracket), because the stop-loss has to be computed from the REAL fill
    price, not an estimate set before the order went live. Once the
    position shows up with no STOP order yet, compute and submit it now."""
    pending = orb_pending_stop.get(symbol)
    if not pending:
        return
    if symbol in open_orders:
        # Stop already exists (shouldn't normally happen for ORB, but safe)
        orb_pending_stop.pop(symbol, None)
        return
    try:
        stop_pct = pending["stop_loss_pct"]
        stop_price = round_to_tick(avg_entry_price * (1 - stop_pct))
        position = trading_client.get_open_position(symbol)
        qty = abs(float(position.qty))
        order = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            stop_price=stop_price,
        )
        trading_client.submit_order(order)
        send_alert(f"ORB stop-loss submitted for {symbol}: entry={avg_entry_price} stop={stop_price}")
        orb_pending_stop.pop(symbol, None)
    except Exception as e:
        logger.error(f"Failed to submit ORB stop-loss for {symbol}: {e}")


def position_manager():
    hwm = load_hwm()
    previous_active_symbols = set()
    bot_initiated_closes = set()
    while True:
        try:
            manager_status["last_heartbeat"] = time.time()
            manager_status["is_alive"] = True

            positions = trading_client.get_all_positions()
            active_symbols = {p.symbol for p in positions}

            externally_closed = previous_active_symbols - active_symbols - bot_initiated_closes
            for sym in externally_closed:
                log_untracked_closure(sym)
            bot_initiated_closes.clear()
            previous_active_symbols = active_symbols

            be_moved.intersection_update(active_symbols)
            quarantined_symbols.intersection_update(active_symbols)

            if positions:
                open_orders = {
                    o.symbol: o for o in trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
                    if o.type == OrderType.STOP
                }
                for pos in positions:
                    symbol = pos.symbol

                    if symbol in quarantined_symbols:
                        continue

                    try:
                        current, entry = float(pos.current_price), float(pos.avg_entry_price)

                        # ORB positions arrive with no STOP order yet — submit
                        # the real one now that we know the actual fill price.
                        if symbol in orb_pending_stop:
                            submit_orb_stop_if_needed(symbol, entry, open_orders)

                        if symbol not in hwm or current > hwm[symbol]:
                            hwm[symbol] = current
                            maybe_save_hwm(hwm)

                        if current >= (entry * (1 + BREAKEVEN_TRIGGER_PCT)) and symbol in open_orders and symbol not in be_moved:
                            order = open_orders[symbol]
                            trading_client.replace_order_by_id(order.id, ReplaceOrderRequest(stop_price=entry))
                            be_moved.add(symbol)
                            logger.info(f"MOVED TO BE: {symbol}")

                        if symbol in be_moved and current <= (hwm[symbol] * TRAIL_GIVEBACK_PCT):
                            logger.info(f"TRAILING HIT: Exiting {symbol}")
                            if symbol in open_orders:
                                trading_client.cancel_order_by_id(open_orders[symbol].id)
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
            logger.warning(f"Transient error: {e}. Retrying...")

        time.sleep(2)


threading.Thread(target=position_manager, daemon=True).start()


@app.route("/health", methods=["GET"])
def health():
    return "OK", 200


# ============================================================
# Existing PMH breakout webhook — unchanged
# ============================================================
@app.route("/", methods=["POST"])
def webhook():
    global recent_signals
    if not manager_status["is_alive"] or (time.time() - manager_status["last_heartbeat"] > 60):
        send_alert("REJECTED SIGNAL: Manager thread offline.")
        return "System Offline", 503
    data = request.get_json(force=True, silent=True)
    if not data:
        return "Bad Request", 400
    if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
        return "Unauthorized", 401
    try:
        symbol = str(data["symbol"]).upper()
        buy_stop = round_to_tick(float(data["buy_stop"]))
        buy_limit, take_profit = round_to_tick(float(data["buy_limit"])), round_to_tick(float(data["take_profit"]))
        stop_loss = round_to_tick(float(data["stop_loss"]))
    except (KeyError, ValueError, TypeError):
        return "Bad Request", 400
    if not (stop_loss < buy_stop <= buy_limit < take_profit):
        return "Bad Request: prices", 400
    if symbol in quarantined_symbols:
        send_alert(f"REJECTED SIGNAL: {symbol} is quarantined pending manual review.")
        return "Symbol quarantined", 200
    if not is_trading_day():
        return "Not a trading day", 200
    if not within_trading_window():
        return "Outside trading hours", 200

    with order_lock:
        now = time.time()
        recent_signals = {s: t for s, t in recent_signals.items() if now - t < SIGNAL_DEDUPE_WINDOW_SECONDS}
        if recent_signals.get(symbol) is not None:
            logger.info(f"Duplicate signal ignored for {symbol}")
            return "Duplicate", 200
        try:
            if any_position_open():
                logger.info(f"Signal ignored for {symbol}: another position/order is already open (one-trade-at-a-time)")
                return "Another position already open", 200

            account = trading_client.get_account()
            qty = calculate_qty(buy_limit, float(account.buying_power))
            if qty < 1:
                send_alert(f"REJECTED: Insufficient buying power for {symbol} at {buy_limit}")
                return "Insufficient buying power", 200

            order = StopLimitOrderRequest(symbol=symbol, qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
                                          stop_price=buy_stop, limit_price=buy_limit, order_class=OrderClass.BRACKET,
                                          take_profit=TakeProfitRequest(limit_price=take_profit),
                                          stop_loss=StopLossRequest(stop_price=stop_loss))
            trading_client.submit_order(order)
            recent_signals[symbol] = now
            send_alert(
                f"Entry placed: {symbol} qty={qty} buy_stop={buy_stop} buy_limit={buy_limit} "
                f"stop_loss={stop_loss} take_profit={take_profit}"
            )
        except Exception as e:
            send_alert(f"CRITICAL: Failed to submit entry for {symbol}: {e}")
            return "Order failed", 500
    return "Success", 200


# ============================================================
# New ORB webhook
# Payload from Pine: {"ticker","action","signal_close","limit_price",
#                      "stop_loss_pct","trailing_pct","cancel_after_sec"}
# No "secret" field in the Pine alert body — auth is via a query-string
# secret instead. Point the TradingView alert's Webhook URL at:
#   https://yourdomain/orb?secret=YOUR_SECRET
# ============================================================
def cancel_if_unfilled(order_id, symbol):
    try:
        order = trading_client.get_order_by_id(order_id)
        if order.status not in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED):
            trading_client.cancel_order_by_id(order_id)
            logger.info(f"Cancelled unfilled ORB entry for {symbol} after timeout")
            send_alert(f"ORB entry for {symbol} cancelled — unfilled after timeout window")
    except Exception as e:
        logger.error(f"Error checking/cancelling ORB entry for {symbol}: {e}")


def get_current_spread(symbol):
    req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
    quote = data_client.get_stock_latest_quote(req)[symbol]
    return float(quote.ask_price) - float(quote.bid_price)


@app.route("/orb", methods=["POST"])
def orb_webhook():
    global recent_signals
    if request.args.get("secret") != ORB_WEBHOOK_SECRET:
        return "Unauthorized", 401
    if not manager_status["is_alive"] or (time.time() - manager_status["last_heartbeat"] > 60):
        send_alert("REJECTED ORB SIGNAL: Manager thread offline.")
        return "System Offline", 503

    data = request.get_json(force=True, silent=True)
    if not data:
        return "Bad Request", 400
    try:
        symbol = str(data["ticker"]).upper()
        action = str(data["action"]).upper()
        limit_price = round_to_tick(float(data["limit_price"]))
        stop_loss_pct = float(data.get("stop_loss_pct", ORB_STOP_LOSS_PCT_DEFAULT * 100)) / 100
        cancel_after_sec = float(data.get("cancel_after_sec", 10))
    except (KeyError, ValueError, TypeError):
        return "Bad Request", 400

    if action != "BUY":
        return "Ignored (non-BUY action)", 200
    if symbol in quarantined_symbols:
        send_alert(f"REJECTED ORB SIGNAL: {symbol} is quarantined pending manual review.")
        return "Symbol quarantined", 200
    if not is_trading_day():
        return "Not a trading day", 200
    if not within_trading_window():
        return "Outside trading hours", 200
    if orb_in_cooldown(symbol):
        logger.info(f"ORB signal for {symbol} ignored: still in cooldown")
        return "Cooldown active", 200

    with order_lock:
        now = time.time()
        recent_signals = {s: t for s, t in recent_signals.items() if now - t < SIGNAL_DEDUPE_WINDOW_SECONDS}
        if recent_signals.get(symbol) is not None:
            logger.info(f"Duplicate ORB signal ignored for {symbol}")
            return "Duplicate", 200
        try:
            if any_position_open():
                logger.info(f"ORB signal ignored for {symbol}: another position/order is already open")
                return "Another position already open", 200

            # Spread check — right before submission, using live L1 quotes.
            # This is the ORB-specific slippage guard Pine can't perform itself.
            spread = get_current_spread(symbol)
            if spread > ORB_SPREAD_MAX:
                send_alert(f"REJECTED ORB SIGNAL: {symbol} spread ${spread:.4f} exceeds ${ORB_SPREAD_MAX:.2f} cap")
                return "Spread too wide", 200

            account = trading_client.get_account()
            qty = calculate_qty(limit_price, float(account.buying_power))
            if qty < 1:
                send_alert(f"REJECTED: Insufficient buying power for {symbol} at {limit_price}")
                return "Insufficient buying power", 200

            # Plain marketable limit order — no bracket. The stop-loss is
            # submitted separately once the real fill price is known.
            order = LimitOrderRequest(
                symbol=symbol, qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, limit_price=limit_price,
            )
            submitted = trading_client.submit_order(order)
            recent_signals[symbol] = now
            orb_pending_stop[symbol] = {"stop_loss_pct": stop_loss_pct}

            threading.Timer(cancel_after_sec, cancel_if_unfilled, args=[submitted.id, symbol]).start()

            send_alert(
                f"ORB entry placed: {symbol} qty={qty} limit={limit_price} "
                f"stop_pct={stop_loss_pct*100:.1f}% cancel_after={cancel_after_sec}s"
            )
        except Exception as e:
            send_alert(f"CRITICAL: Failed to submit ORB entry for {symbol}: {e}")
            return "Order failed", 500
    return "Success", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), threaded=True)


























































































































