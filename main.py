import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from logging.handlers import RotatingFileHandler

from flask import Flask, request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    ReplaceOrderRequest,
    GetOrdersRequest,
    LimitOrderRequest,
    TakeProfitRequest,
    StopLossRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, OrderType, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from requests.exceptions import Timeout, ConnectionError

# ============================================================
# Logging — persistent rotating file log + console
# ============================================================
log_handler = RotatingFileHandler("bot.log", maxBytes=10 * 1024 * 1024, backupCount=10)
log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(
    level=logging.INFO,
    handlers=[log_handler, logging.StreamHandler()],
)
logger = logging.getLogger("trading_bot")

app = Flask(__name__)

is_live = os.getenv("LIVE_TRADING", "False") == "True"
logger.info(f"--- BOT STARTED: LIVE_TRADING={is_live} ---")
trading_client = TradingClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"), paper=not is_live)
# Separate client for live market data (used to sanity-check a signal
# against the real current price before committing an order).
data_client = StockHistoricalDataClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"))

HWM_FILE = "hwm_data.json"
bot_start_time = time.time()
manager_status = {"last_heartbeat": time.time(), "is_alive": True}
symbol_error_counts = {}
symbol_alert_cooldown = {}
ERROR_ALERT_INTERVAL = 10

# An unfilled entry order can otherwise sit indefinitely (up to end of day)
# holding the single concurrency slot hostage, blocking fresh signals on
# other tickers that might actually move. Cancel unfilled BUY entry orders
# once they've been open this long, freeing the slot back up.
ENTRY_ORDER_TIMEOUT_SECONDS = int(os.getenv("ENTRY_ORDER_TIMEOUT_SECONDS", "600"))  # 10 min default

# Risk / gating config — window start 09:45 ET, close entries at 16:00 ET
TRADING_WINDOW_START = os.getenv("TRADING_WINDOW_START", "09:00")
TRADING_WINDOW_END = os.getenv("TRADING_WINDOW_END", "11:30")
NY_TZ = ZoneInfo("America/New_York")

# Position sizing — qty is computed server-side from available equity rather
# than supplied by Pine. Full-size by design; the candle-range / stop-distance
# checks below are the risk control, not position size.
EQUITY_FRACTION = float(os.getenv("EQUITY_FRACTION", "0.98"))

# Defense-in-depth: reject any signal whose implied stop distance exceeds
# this, regardless of what Pine computed — full-size sizing means this is
# the only backstop against an outlier candle producing an outsized loss.
MAX_STOP_DISTANCE_PCT = float(os.getenv("MAX_STOP_DISTANCE_PCT", "15.0"))

# Guards against a signal firing on a condition that was true at bar-close
# evaluation time but no longer reflects reality (late/revised tape prints
# on thin small caps, TradingView's server-side data snapshot diverging
# slightly from the live feed). Reject if the current live price has
# already drifted this far from what the signal computed as entry_limit.
STALE_SIGNAL_MAX_DRIFT_PCT = float(os.getenv("STALE_SIGNAL_MAX_DRIFT_PCT", "2.0"))

# Flat trailing-stop percentage. The stop only ever ratchets UP (native STOP
# order price is replaced, never loosened) toward highest-price-since-entry
# minus this percent. Take-profit (HTF AVWAP, computed in Pine) is the
# primary exit target; this is the fallback if that target is never reached.
TRAIL_PERCENT = float(os.getenv("TRAIL_PERCENT", "15.0"))

# Breakeven guarantee: once the position is up this much, immediately move
# the stop to entry, independent of what the trail calc alone would give.
# The 15% trail doesn't mathematically reach entry until the position is up
# ~17.6% (entry / 0.85), so without this a trade could run up double-digits
# and give the whole move back before anything protects it.
BREAKEVEN_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "2.0"))

# Idempotency & deduplication
order_lock = threading.Lock()
hwm_lock = threading.RLock()
recent_signals = {}
SIGNAL_DEDUPE_WINDOW_SECONDS = 15

# HWM persistent storage state
HWM_SAVE_INTERVAL_SECONDS = 5
_last_hwm_save = 0.0
_last_saved_hwm_values = {}
global_hwm = {}


def is_transient_error(e):
    if isinstance(e, (Timeout, ConnectionError)):
        return True
    err_str = str(e)
    transient_indicators = ["429", "500", "502", "503", "504", "timeout", "reset", "temporarily unavailable"]
    return any(indicator in err_str for indicator in transient_indicators)


def load_hwm():
    global global_hwm, _last_saved_hwm_values
    with hwm_lock:
        if os.path.exists(HWM_FILE):
            try:
                with open(HWM_FILE, "r") as f:
                    data = json.load(f)
                    global_hwm = data
                    _last_saved_hwm_values = data.copy()
                    return data
            except Exception:
                global_hwm = {}
                return {}
        global_hwm = {}
        return {}


def save_hwm(hwm):
    global _last_saved_hwm_values
    with hwm_lock:
        try:
            with open(HWM_FILE, "w") as f:
                json.dump(hwm, f)
            _last_saved_hwm_values = hwm.copy()
        except Exception as e:
            logger.error(f"Failed to save HWM data: {e}")


def maybe_save_hwm(hwm, symbol=None, current_price=0.0, force=False):
    global _last_hwm_save
    now = time.time()

    should_save = force
    if not should_save and (now - _last_hwm_save) >= HWM_SAVE_INTERVAL_SECONDS:
        should_save = True

    if not should_save and symbol and symbol in hwm:
        last_saved = _last_saved_hwm_values.get(symbol, 0.0)
        if last_saved == 0.0 or current_price >= last_saved * 1.01:
            should_save = True

    if should_save:
        save_hwm(hwm)
        _last_hwm_save = now


def send_alert(message):
    logger.info(f"ALERT: {message}")


def emergency_flatten():
    send_alert("[CRIT] Emergency Flatten Triggered!")
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
        send_alert(f"[CRIT] Symbol {symbol} failing repeatedly ({count} consecutive errors).")
        symbol_alert_cooldown[symbol] = count
    elif count > 3 and (count - symbol_alert_cooldown.get(symbol, 3)) >= ERROR_ALERT_INTERVAL:
        send_alert(f"[CRIT] Symbol {symbol} still failing ({count} consecutive errors).")
        symbol_alert_cooldown[symbol] = count


def within_trading_window():
    now_ny = datetime.now(NY_TZ).time()
    start = datetime.strptime(TRADING_WINDOW_START, "%H:%M").time()
    end = datetime.strptime(TRADING_WINDOW_END, "%H:%M").time()
    return start <= now_ny <= end


def has_open_exposure(symbol):
    positions = trading_client.get_all_positions()
    if any(p.symbol == symbol for p in positions):
        return True

    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    if any(o.symbol == symbol and o.side == OrderSide.BUY for o in open_orders):
        return True

    return False


def has_any_open_exposure():
    """Hard cap of 1 concurrent position/pending entry across ALL symbols."""
    positions = trading_client.get_all_positions()
    if len(positions) > 0:
        return True

    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))
    if any(o.side == OrderSide.BUY for o in open_orders):
        return True

    return False


def retry_replace_order(order_id, replace_request, retries=3, delay=1.0):
    for attempt in range(1, retries + 1):
        try:
            return trading_client.replace_order_by_id(order_id, replace_request)
        except Exception as e:
            if attempt == retries or not is_transient_error(e):
                raise
            logger.warning(f"Transient error replacing order {order_id} (attempt {attempt}/{retries}): {e}. Retrying in {delay}s...")
            time.sleep(delay)


def retry_cancel_order(order_id, retries=3, delay=1.0):
    for attempt in range(1, retries + 1):
        try:
            return trading_client.cancel_order_by_id(order_id)
        except Exception as e:
            if attempt == retries or not is_transient_error(e):
                raise
            logger.warning(f"Transient error cancelling order {order_id} (attempt {attempt}/{retries}): {e}. Retrying in {delay}s...")
            time.sleep(delay)


def position_manager_loop():
    global global_hwm
    global_hwm = load_hwm()
    last_equity_log = 0.0
    eod_flatten_triggered_date = None
    # Tracks currently-open positions so we can detect the moment one closes
    # (TP fill, trailing-stop fill, or EOD flatten) and log a single clean
    # summary line instead of noisy per-minute updates.
    open_positions_tracked = {}  # symbol -> {"qty", "entry_price"}

    while True:
        try:
            manager_status["last_heartbeat"] = time.time()
            manager_status["is_alive"] = True
            now = time.time()
            now_ny_dt = datetime.now(NY_TZ)

            # EOD Hard Close: Flatten all positions and cancel open orders at 15:55 ET
            current_date_str = now_ny_dt.strftime("%Y-%m-%d")
            current_time_val = now_ny_dt.time()
            eod_target_time = datetime.strptime("15:55", "%H:%M").time()

            if current_time_val >= eod_target_time and eod_flatten_triggered_date != current_date_str:
                send_alert("[EOD] Reached 15:55 ET. Flattening all positions and cancelling open orders.")
                try:
                    trading_client.close_all_positions(cancel_orders=True)
                    with hwm_lock:
                        global_hwm.clear()
                        maybe_save_hwm(global_hwm, force=True)
                    send_alert("[EOD] Successfully closed all positions and cleared HWM.")
                except Exception as e:
                    send_alert(f"[CRIT] EOD Flatten failed: {e}")
                eod_flatten_triggered_date = current_date_str

            if now - last_equity_log >= 3600:
                try:
                    account = trading_client.get_account()
                    logger.info(f"ACCOUNT EQUITY UPDATE: Total Equity = ${float(account.equity):.2f}, Buying Power = ${float(account.buying_power):.2f}")
                    last_equity_log = now
                except Exception as e:
                    logger.warning(f"Failed to log account equity: {e}")

            positions = trading_client.get_all_positions()
            current_symbols = {p.symbol for p in positions}

            # Trade-close detection: anything we were tracking that's no
            # longer an open position just closed -- via the native TP fill,
            # the trailing STOP fill, or EOD flatten. Alpaca's own bracket
            # OCO linkage cancels whichever sibling leg didn't fire, so
            # there's no stray-order cleanup needed here (unlike the earlier
            # AMIX bug, which came from a manual close_position() call
            # stepping on a still-resting sibling leg -- this design never
            # calls close_position() for a normal exit, only ever replaces
            # the trailing stop's price, so that failure mode can't recur).
            for symbol in set(open_positions_tracked.keys()) - current_symbols:
                info = open_positions_tracked.pop(symbol)
                try:
                    recent_orders = trading_client.get_orders(
                        filter=GetOrdersRequest(status=QueryOrderStatus.CLOSED, symbols=[symbol], limit=10)
                    )
                    exit_order = next(
                        (o for o in recent_orders if o.side == OrderSide.SELL and o.filled_avg_price),
                        None,
                    )
                    if exit_order:
                        exit_price = float(exit_order.filled_avg_price)
                        qty = float(exit_order.filled_qty or info["qty"])
                        pl = (exit_price - info["entry_price"]) * qty
                        pl_pct = ((exit_price / info["entry_price"]) - 1) * 100
                        exit_time = exit_order.filled_at.strftime("%H:%M:%S") if exit_order.filled_at else "?"
                        tag = "[WIN]" if pl >= 0 else "[LOSS]"
                        logger.info(
                            f"{tag} {symbol} entry ${info['entry_price']:.4f} -> "
                            f"exit ${exit_price:.4f} @ {exit_time} | qty {qty:.0f} | "
                            f"P/L: ${pl:.2f} ({pl_pct:+.2f}%) | exit: {exit_order.type}"
                        )
                    else:
                        logger.info(f"[?] {symbol} closed | qty {info['qty']:.0f} | (exit fill details unavailable)")
                except Exception as e:
                    logger.warning(f"Could not log close summary for {symbol}: {e}")

                symbol_error_counts[symbol] = 0
                symbol_alert_cooldown.pop(symbol, None)
                with hwm_lock:
                    if symbol in global_hwm:
                        del global_hwm[symbol]
                        maybe_save_hwm(global_hwm, force=True)

            # Track newly-opened positions with their true entry fill price.
            for p in positions:
                if p.symbol not in open_positions_tracked:
                    open_positions_tracked[p.symbol] = {
                        "qty": float(p.qty),
                        "entry_price": float(p.avg_entry_price),
                    }

            all_open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))

            open_orders_map = {}
            for o in all_open_orders:
                if o.type == OrderType.STOP:
                    open_orders_map.setdefault(o.symbol, []).append(o)

            # Stale entry order cleanup: an unfilled BUY entry that's been
            # sitting too long blocks the concurrency cap from freeing up.
            for o in all_open_orders:
                if o.side == OrderSide.BUY:
                    age_seconds = (datetime.now(timezone.utc) - o.created_at).total_seconds()
                    if age_seconds >= ENTRY_ORDER_TIMEOUT_SECONDS:
                        try:
                            retry_cancel_order(o.id)
                            send_alert(
                                f"[TIMEOUT] Cancelled stale unfilled entry for {o.symbol} after "
                                f"{age_seconds:.0f}s ({ENTRY_ORDER_TIMEOUT_SECONDS}s timeout) — "
                                f"slot freed for new signals."
                            )
                        except Exception as e:
                            send_alert(f"[CRIT] Failed to cancel stale entry order {o.id} for {o.symbol}: {e}")

            for pos in positions:
                symbol = pos.symbol
                try:
                    current, entry = float(pos.current_price), float(pos.avg_entry_price)

                    with hwm_lock:
                        if symbol not in global_hwm or current > global_hwm[symbol]:
                            global_hwm[symbol] = current
                            maybe_save_hwm(global_hwm, symbol=symbol, current_price=current)
                        hwm_val = global_hwm.get(symbol, current)

                    # Stop management: two floors combine, whichever is
                    # higher wins (the stop only ever ratchets up).
                    # 1) Breakeven: once up BREAKEVEN_TRIGGER_PCT%, the stop
                    #    must be at least entry price, regardless of the
                    #    trail calc -- the 15% trail alone doesn't reach
                    #    entry until the position is up ~17.6%.
                    # 2) 15% trail: (highest price since entry) x 0.85,
                    #    the fallback exit if TP is never reached.
                    # Alpaca's own engine fires the STOP the instant price
                    # crosses it -- the bot's only job is keeping the price
                    # current.
                    if symbol in open_orders_map:
                        trail_level = hwm_val * (1 - TRAIL_PERCENT / 100)
                        desired_stop = trail_level
                        breakeven_earned = current >= entry * (1 + BREAKEVEN_TRIGGER_PCT / 100)
                        if breakeven_earned:
                            desired_stop = max(desired_stop, entry)

                        for order in open_orders_map[symbol]:
                            current_stop = float(order.stop_price)
                            if desired_stop > current_stop:
                                try:
                                    retry_replace_order(
                                        order.id, ReplaceOrderRequest(stop_price=round(desired_stop, 4))
                                    )
                                    tag = "[BE]" if breakeven_earned and desired_stop == entry else "[TRAIL]"
                                    logger.info(f"{tag} {symbol} stop -> ${desired_stop:.4f} (HWM ${hwm_val:.4f})")
                                except Exception as e:
                                    send_alert(f"[CRIT] Failed to move stop for {symbol}: {e}")

                except Exception as e:
                    handle_symbol_error(symbol, e)

        except Exception as e:
            if any(code in str(e) for code in ["401", "403"]):
                emergency_flatten()
                break
            logger.warning(f"Transient error in position manager: {e}. Retrying...")

        time.sleep(2)


def position_manager():
    while True:
        try:
            position_manager_loop()
        except Exception as e:
            logger.exception(f"Position manager crashed: {e}")
            manager_status["is_alive"] = False
            time.sleep(5)


threading.Thread(target=position_manager, daemon=True).start()


def handle_shutdown(signum, frame):
    logger.info("Shutdown signal received. Saving in-memory HWM...")
    with hwm_lock:
        save_hwm(global_hwm)
    sys.exit(0)

signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint monitoring thread liveness, metrics, and uptime."""
    alive = manager_status["is_alive"]
    heartbeat_age = time.time() - manager_status["last_heartbeat"]
    is_healthy = alive and heartbeat_age < 30

    try:
        positions = trading_client.get_all_positions()
        positions_count = len(positions)
    except Exception:
        positions_count = 0

    uptime_hours = (time.time() - bot_start_time) / 3600.0

    payload = {
        "alive": alive,
        "heartbeat_age": round(heartbeat_age, 2),
        "positions": positions_count,
        "uptime_hours": round(uptime_hours, 2)
    }

    if not is_healthy:
        return payload, 500
    return payload, 200


@app.route("/", methods=["POST"])
def webhook():
    start_time = time.time()

    if not manager_status["is_alive"] or (time.time() - manager_status["last_heartbeat"] > 60):
        send_alert("[CRIT] Manager thread offline — signal rejected.")
        return "System Offline", 503

    data = request.get_json(force=True, silent=True)
    if not data:
        send_alert("[REJECT] No/invalid JSON body received.")
        return "Bad Request: no JSON body", 400

    if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
        send_alert("[REJECT] Invalid webhook secret.")
        return "Unauthorized", 401

    # take_profit is back (HTF AVWAP target, computed in Pine). No buy_stop --
    # there's no future trigger to wait for, the signal already confirmed at
    # bar close. qty is still server-computed from equity.
    try:
        symbol = str(data["symbol"]).upper()
        entry_limit = float(data["entry_limit"])
        stop_loss = float(data["stop_loss"])
        take_profit = float(data["take_profit"])
    except (KeyError, ValueError, TypeError) as e:
        send_alert(f"[REJECT] Malformed payload — {e}")
        return "Bad Request: malformed payload", 400

    if not (0 < stop_loss < entry_limit < take_profit):
        send_alert(
            f"[REJECT] {symbol} levels out of order — "
            f"stop_loss={stop_loss}, entry_limit={entry_limit}, take_profit={take_profit}"
        )
        return "Bad Request: price levels out of order", 400

    # Defense-in-depth: reject regardless of what Pine computed if the
    # implied stop distance is outside a sane ceiling.
    stop_distance_pct = ((entry_limit - stop_loss) / entry_limit) * 100
    if stop_distance_pct > MAX_STOP_DISTANCE_PCT:
        send_alert(
            f"[REJECT] {symbol} stop distance {stop_distance_pct:.2f}% exceeds "
            f"MAX_STOP_DISTANCE_PCT={MAX_STOP_DISTANCE_PCT}%."
        )
        return "Bad Request: stop distance too wide", 400

    if not within_trading_window():
        send_alert(f"[REJECT] {symbol} arrived outside trading window ({TRADING_WINDOW_START}-{TRADING_WINDOW_END} ET).")
        return "Outside trading hours", 200

    with order_lock:
        now = time.time()

        global recent_signals
        recent_signals = {
            s: t for s, t in recent_signals.items() if now - t < SIGNAL_DEDUPE_WINDOW_SECONDS
        }

        if recent_signals.get(symbol) is not None:
            send_alert(f"[SKIP] Duplicate signal for {symbol} — received again within {SIGNAL_DEDUPE_WINDOW_SECONDS}s.")
            return "Duplicate (rate-limited)", 200

        try:
            if has_any_open_exposure():
                send_alert(f"[SKIP] Signal for {symbol} ignored — max concurrent positions (cap: 1).")
                return "Max concurrent positions reached", 200
        except Exception as e:
            send_alert(f"[CRIT] Could not verify concurrency cap for {symbol}, rejecting for safety: {e}")
            return "Concurrency check failed", 500

        try:
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
        except Exception as e:
            send_alert(f"[CRIT] Could not check buying power for {symbol}, rejecting for safety: {e}")
            return "Buying power check failed", 500

        qty = int((buying_power * EQUITY_FRACTION) // entry_limit)
        if qty < 1:
            send_alert(
                f"[REJECT] {symbol} insufficient buying power to size a position — "
                f"buying_power=${buying_power:.2f}, entry_limit={entry_limit}"
            )
            return "Insufficient buying power", 200

        # Stale-signal check: verify the live quote hasn't already drifted
        # meaningfully away from what the confirmed signal computed. This
        # is the last line of defense against a condition that was true at
        # bar-close evaluation but no longer reflects the real market —
        # deliberately checked right before submission, using the freshest
        # possible price, not earlier in the request.
        try:
            quote_req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quote = data_client.get_stock_latest_quote(quote_req)[symbol]
            live_price = float(quote.ask_price) if quote.ask_price else float(quote.bid_price)
            if not live_price or live_price <= 0:
                raise ValueError(f"no usable live price (ask={quote.ask_price}, bid={quote.bid_price})")
        except Exception as e:
            send_alert(f"[CRIT] Could not verify live price for {symbol}, rejecting for safety: {e}")
            return "Live price check failed", 500

        drift_pct = abs(live_price - entry_limit) / entry_limit * 100
        if drift_pct > STALE_SIGNAL_MAX_DRIFT_PCT:
            send_alert(
                f"[REJECT] {symbol} signal appears stale — live price ${live_price:.4f} is "
                f"{drift_pct:.2f}% away from entry_limit ${entry_limit:.4f} "
                f"(max {STALE_SIGNAL_MAX_DRIFT_PCT}%)."
            )
            return "Stale signal rejected", 200

        try:
            # BRACKET: entry + take_profit (HTF AVWAP) + stop_loss (candle
            # low, then continuously ratcheted up as a 15% trail). Alpaca
            # links these as OCO siblings natively -- whichever fires,
            # the other is auto-cancelled on Alpaca's side. The bot never
            # manually closes the position for this exit, so the AMIX
            # stray-order bug class doesn't apply here.
            order = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=entry_limit,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=take_profit),
                stop_loss=StopLossRequest(stop_price=stop_loss),
            )
            submitted = trading_client.submit_order(order)
            recent_signals[symbol] = now

            latency_ms = (time.time() - start_time) * 1000
            send_alert(
                f"[ENTRY] {symbol} qty={qty} (equity-sized) limit={entry_limit} "
                f"TP={take_profit} SL={stop_loss} (trailing {TRAIL_PERCENT}%) "
                f"| Order ID: {submitted.id} | Client Order ID: {submitted.client_order_id} | "
                f"Status: {submitted.status} | Latency: {latency_ms:.1f}ms"
            )
            logger.info(f"Order submitted: {submitted.id} (Client ID: {submitted.client_order_id}) for {symbol} with status {submitted.status}")
        except Exception as e:
            send_alert(f"[CRIT] Failed to submit entry order for {symbol}: {e}")
            return "Order submission failed", 500

    return "Success", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
























































































































