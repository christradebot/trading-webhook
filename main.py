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
    MarketOrderRequest,
    StopOrderRequest,
    TakeProfitRequest,
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

# An unfilled entry order sits as a resting limit at the AVWAP level. Per
# the "quick continuation off the level" design, if it hasn't filled within
# 5 minutes the setup is considered dead -- cancel and free the slot for
# the next signal rather than leaving it hoping for a late fill.
ENTRY_ORDER_TIMEOUT_SECONDS = int(os.getenv("ENTRY_ORDER_TIMEOUT_SECONDS", "300"))  # 5 min default

# Risk / gating config — entry window 09:05-11:35 ET
TRADING_WINDOW_START = os.getenv("TRADING_WINDOW_START", "09:05")
TRADING_WINDOW_END = os.getenv("TRADING_WINDOW_END", "11:35")
NY_TZ = ZoneInfo("America/New_York")

# Position sizing — qty is computed server-side from available equity rather
# than supplied by Pine. Full-size by design; the candle-range / stop-distance
# checks below are the risk control, not position size.
EQUITY_FRACTION = float(os.getenv("EQUITY_FRACTION", "0.98"))

# Defense-in-depth: reject any signal whose implied stop distance (using
# the signal candle's low Pine sends, even though it's no longer submitted
# as a resting order -- see below) exceeds this. Purely a pre-entry quality
# filter now, not protecting a real resting order.
MAX_STOP_DISTANCE_PCT = float(os.getenv("MAX_STOP_DISTANCE_PCT", "15.0"))

# Guards against the setup being invalidated before the order can be
# placed. Entry is a resting limit at a specific AVWAP level (buying the
# pullback back to the level, not chasing current price), so price being
# ABOVE entry_limit at submission time is normal and expected. What
# actually invalidates the setup is price having already breached the
# signal candle's low (stop_loss, still sent for reference even though
# it's no longer a resting order) before the order could even be placed.
STALE_SIGNAL_STOP_BUFFER_PCT = float(os.getenv("STALE_SIGNAL_STOP_BUFFER_PCT", "0.0"))

# Flat trailing-stop percentage. The stop only ever ratchets UP (native STOP
# order price is replaced, never loosened) toward highest-price-since-entry
# minus this percent. Take-profit (HTF AVWAP, computed in Pine) is the
# primary exit target; this is the fallback if that target is never reached.
TRAIL_PERCENT = float(os.getenv("TRAIL_PERCENT", "15.0"))

# Gain threshold that switches the exit mechanism entirely. Below this, a
# position has NO resting exchange-side stop at all -- the EMA-close signal
# from Pine is the only stop, executed as a market sell (see EMA_EXIT
# handling in the webhook). At/above this, a native trailing STOP order is
# created for the first time and takes over completely; the EMA-close
# signal is ignored from that point on. This mirrors "trade with the trend
# while it's proving itself, protect the gain with a hard price floor once
# it's real."
TRAIL_ACTIVATION_GAIN_PCT = float(os.getenv("TRAIL_ACTIVATION_GAIN_PCT", "15.0"))

# Breakeven guarantee: once the position is up this much, immediately move
# the stop to entry, independent of what the trail calc alone would give.
# The 15% trail doesn't mathematically reach entry until the position is up
# ~17.6% (entry / 0.85), so without this a trade could run up double-digits
# and give the whole move back before anything protects it.
BREAKEVEN_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "2.0"))

# Second breakeven tier: a flat floor at breakeven has the same gap problem
# one level up -- once a trade has run to +8%+, it's proven real strength,
# but the floor still sits pinned at exactly entry until the trail reaches
# ~17.6%. That leaves an +8%-to-+17.6% stretch where a normal pullback wipes
# out the entire gain back to breakeven instead of locking in any of it.
# This tier locks in a real profit cushion once that strength is shown.
BREAKEVEN_TIER2_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TIER2_TRIGGER_PCT", "8.0"))
BREAKEVEN_TIER2_LOCK_PCT = float(os.getenv("BREAKEVEN_TIER2_LOCK_PCT", "4.0"))

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

# ============================================================
# Reject-reason tally — reset daily at EOD flatten, logged as one
# summary line so you can see the day's rejection breakdown at a
# glance instead of scrolling the raw log (e.g. "6x WINDOW, 2x
# TP_NOT_SET, 1x STALE"). Every reject() call below increments this.
# ============================================================
reject_tally_lock = threading.Lock()
reject_reason_counts = {}


def reject(tag, symbol, message, http_status=200, log_level="info"):
    """Central reject helper. Every webhook rejection MUST go through this
    so the reason is both tagged distinctly in the log and tallied for the
    daily summary. `tag` should be a short stable code (e.g. "WINDOW",
    "TP_NOT_SET") — not a full sentence — so the tally groups correctly."""
    with reject_tally_lock:
        reject_reason_counts[tag] = reject_reason_counts.get(tag, 0) + 1
    full_message = f"[REJECT:{tag}] {symbol} — {message}"
    if log_level == "error":
        logger.error(full_message)
    else:
        logger.info(f"ALERT: {full_message}")
    return message, http_status


def log_reject_summary():
    """Logs a single tallied line of today's reject reasons, then resets
    the counter for the next session. Called from the EOD flatten path."""
    with reject_tally_lock:
        if not reject_reason_counts:
            logger.info("[SUMMARY] No rejected signals today.")
        else:
            parts = ", ".join(f"{count}x {tag}" for tag, count in sorted(reject_reason_counts.items(), key=lambda kv: -kv[1]))
            logger.info(f"[SUMMARY] Reject reasons today: {parts}")
        reject_reason_counts.clear()


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
                # Daily reject-reason summary, right alongside the flatten —
                # one glance tells you how many signals fired vs. how many
                # were rejected and why, before the tally resets for tomorrow.
                log_reject_summary()
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

                    # Stop management, in two phases:
                    # PHASE 1 (gain < TRAIL_ACTIVATION_GAIN_PCT): no native
                    # stop order exists at all. The EMA-close signal from
                    # Pine, handled in the webhook's EMA_EXIT branch, is the
                    # only stop mechanism here -- trading with the trend,
                    # giving room for normal pullbacks instead of a fixed
                    # price floor.
                    # PHASE 2 (gain >= TRAIL_ACTIVATION_GAIN_PCT): a native
                    # STOP order is created for the first time (if it
                    # doesn't exist yet) at whichever is highest of the
                    # trail level or breakeven tiers, then ratcheted up each
                    # cycle exactly as before. The EMA-close signal is
                    # ignored by the webhook from this point on.
                    if current >= entry * (1 + TRAIL_ACTIVATION_GAIN_PCT / 100):
                        trail_level = hwm_val * (1 - TRAIL_PERCENT / 100)
                        desired_stop = trail_level
                        be_tag = None

                        if current >= entry * (1 + BREAKEVEN_TIER2_TRIGGER_PCT / 100):
                            tier2_floor = entry * (1 + BREAKEVEN_TIER2_LOCK_PCT / 100)
                            if tier2_floor > desired_stop:
                                desired_stop = tier2_floor
                                be_tag = "[BE2]"
                        elif current >= entry * (1 + BREAKEVEN_TRIGGER_PCT / 100):
                            if entry > desired_stop:
                                desired_stop = entry
                                be_tag = "[BE]"

                        if symbol in open_orders_map:
                            for order in open_orders_map[symbol]:
                                current_stop = float(order.stop_price)
                                if desired_stop > current_stop:
                                    try:
                                        retry_replace_order(
                                            order.id, ReplaceOrderRequest(stop_price=round(desired_stop, 4))
                                        )
                                        tag = be_tag or "[TRAIL]"
                                        logger.info(f"{tag} {symbol} stop -> ${desired_stop:.4f} (HWM ${hwm_val:.4f})")
                                    except Exception as e:
                                        send_alert(f"[CRIT] Failed to move stop for {symbol}: {e}")
                        else:
                            # First time this position has crossed the gain
                            # threshold -- no native stop exists yet, create
                            # one now. From the next loop iteration on it'll
                            # show up in open_orders_map and just ratchet
                            # via the branch above like any other position.
                            try:
                                stop_order = StopOrderRequest(
                                    symbol=symbol,
                                    qty=pos.qty,
                                    side=OrderSide.SELL,
                                    time_in_force=TimeInForce.DAY,
                                    stop_price=round(desired_stop, 4),
                                )
                                submitted_stop = trading_client.submit_order(stop_order)
                                tag = be_tag or "[TRAIL]"
                                send_alert(
                                    f"{tag} {symbol} crossed +{TRAIL_ACTIVATION_GAIN_PCT}% gain -- created native "
                                    f"stop at ${desired_stop:.4f} (HWM ${hwm_val:.4f}) | Order ID: {submitted_stop.id}. "
                                    f"EMA-close exit signal will now be ignored for this symbol."
                                )
                            except Exception as e:
                                send_alert(f"[CRIT] Failed to create initial trailing stop for {symbol}: {e}")

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


@app.route("/reject-summary", methods=["GET"])
def reject_summary():
    """On-demand snapshot of today's reject tally so far, without waiting
    for the EOD flatten to see it. Doesn't reset the counter."""
    with reject_tally_lock:
        return dict(reject_reason_counts), 200


@app.route("/", methods=["POST"])
def webhook():
    start_time = time.time()

    if not manager_status["is_alive"] or (time.time() - manager_status["last_heartbeat"] > 60):
        send_alert("[CRIT] Manager thread offline — signal rejected.")
        return "System Offline", 503

    data = request.get_json(force=True, silent=True)
    if not data:
        # No symbol available yet at this point (body itself is missing/invalid) —
        # most of these are TradingView's periodic webhook connectivity test
        # pings, not real signals. Tallied separately so they don't get
        # confused with genuine rejected trade signals in the summary.
        return reject("NO_JSON", "-", "no/invalid JSON body received", http_status=400)

    if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
        return reject("BAD_SECRET", data.get("symbol", "-"), "invalid webhook secret", http_status=401)

    symbol = str(data.get("symbol", "UNKNOWN")).upper()
    signal_type = data.get("type", "entry")

    # ============================================================
    # EMA-close exit signal -- handled entirely separately from the
    # entry flow below, and deliberately BEFORE the trading-window
    # check, since exiting an open position must be allowed at any
    # time, not just during the entry window (matches EOD flatten
    # already working this way).
    # ============================================================
    if signal_type == "exit_ema_close":
        with order_lock:
            try:
                positions = trading_client.get_all_positions()
                pos = next((p for p in positions if p.symbol == symbol), None)
            except Exception as e:
                return reject("EMA_EXIT_CHECK_FAILED", symbol, f"could not check open positions: {e}", http_status=500, log_level="error")

            if pos is None:
                logger.info(f"[EMA_EXIT] {symbol} — signal received but no open position, ignoring.")
                return "No open position", 200

            entry_price = float(pos.avg_entry_price)
            current_price = float(pos.current_price)
            gain_pct = ((current_price / entry_price) - 1) * 100

            if gain_pct >= TRAIL_ACTIVATION_GAIN_PCT:
                logger.info(
                    f"[EMA_EXIT] {symbol} — signal received but gain is {gain_pct:.2f}% "
                    f"(>= {TRAIL_ACTIVATION_GAIN_PCT}%), trailing stop already governs this position, ignoring."
                )
                return "Gain past threshold, trailing stop governs", 200

            try:
                open_orders = trading_client.get_orders(
                    filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
                )
                for o in open_orders:
                    retry_cancel_order(o.id)
            except Exception as e:
                send_alert(f"[CRIT] EMA_EXIT: failed cancelling resting orders for {symbol} before flatten: {e}")

            try:
                sell_order = MarketOrderRequest(
                    symbol=symbol,
                    qty=pos.qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
                submitted = trading_client.submit_order(sell_order)
                send_alert(
                    f"[EMA_EXIT] {symbol} closed at market — gain was {gain_pct:.2f}%, "
                    f"close below 9 EMA | Order ID: {submitted.id}"
                )
            except Exception as e:
                send_alert(f"[CRIT] EMA_EXIT: failed to submit market sell for {symbol}: {e}")
                return "Exit order submission failed", 500

        return "Exit executed", 200

    try:
        entry_limit = float(data["entry_limit"])
        stop_loss = float(data["stop_loss"])
        take_profit = float(data["take_profit"])
    except (KeyError, ValueError, TypeError) as e:
        return reject("MALFORMED", symbol, f"malformed payload — {e}", http_status=400)

    # ============================================================
    # Trading window check now runs FIRST, right after the payload is
    # parseable — before any level validation. Previously this ran last,
    # which meant a signal that was both outside the window AND had bad
    # levels (e.g. an unset take_profit) would get logged only as
    # "levels out of order", hiding the fact that it was also a stale/
    # out-of-window signal. Checking window first makes the log tell you
    # the real first-order reason every time.
    # ============================================================
    if not within_trading_window():
        now_ny_str = datetime.now(NY_TZ).strftime("%H:%M:%S")
        return reject(
            "WINDOW", symbol,
            f"arrived at {now_ny_str} ET, outside window {TRADING_WINDOW_START}-{TRADING_WINDOW_END} ET "
            f"(entry_limit={entry_limit}, stop_loss={stop_loss}, take_profit={take_profit})",
        )

    # ============================================================
    # Level ordering — split into distinct sub-checks so the log says
    # exactly which value broke it, instead of one generic message.
    # take_profit == 0.0 is called out by name since that's the most
    # common real-world cause: the Pine "Take Profit Target Price"
    # input wasn't set on that ticker's chart before the session.
    # ============================================================
    if not (stop_loss > 0):
        return reject("SL_INVALID", symbol, f"stop_loss must be positive (got {stop_loss})")

    if not (stop_loss < entry_limit):
        return reject("SL_GE_ENTRY", symbol, f"stop_loss ({stop_loss}) must be below entry_limit ({entry_limit})")

    if take_profit == 0.0:
        return reject(
            "TP_NOT_SET", symbol,
            f"take_profit is 0.0 — the 'Take Profit Target Price' input likely wasn't set on this "
            f"ticker's chart before the session (entry_limit={entry_limit}, stop_loss={stop_loss})",
        )

    if not (entry_limit < take_profit):
        return reject("TP_LE_ENTRY", symbol, f"entry_limit ({entry_limit}) must be below take_profit ({take_profit})")

    # Defense-in-depth: reject regardless of what Pine computed if the
    # implied stop distance is outside a sane ceiling.
    stop_distance_pct = ((entry_limit - stop_loss) / entry_limit) * 100
    if stop_distance_pct > MAX_STOP_DISTANCE_PCT:
        return reject(
            "STOP_TOO_WIDE", symbol,
            f"stop distance {stop_distance_pct:.2f}% exceeds MAX_STOP_DISTANCE_PCT={MAX_STOP_DISTANCE_PCT}%",
        )

    with order_lock:
        now = time.time()

        global recent_signals
        recent_signals = {
            s: t for s, t in recent_signals.items() if now - t < SIGNAL_DEDUPE_WINDOW_SECONDS
        }

        if recent_signals.get(symbol) is not None:
            return reject("DUPLICATE", symbol, f"duplicate signal received again within {SIGNAL_DEDUPE_WINDOW_SECONDS}s")

        try:
            if has_any_open_exposure():
                return reject("CONCURRENCY_CAP", symbol, "max concurrent positions reached (cap: 1)")
        except Exception as e:
            return reject("CONCURRENCY_CHECK_FAILED", symbol, f"could not verify concurrency cap, rejecting for safety: {e}", http_status=500, log_level="error")

        try:
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
        except Exception as e:
            return reject("BUYING_POWER_CHECK_FAILED", symbol, f"could not check buying power, rejecting for safety: {e}", http_status=500, log_level="error")

        qty = int((buying_power * EQUITY_FRACTION) // entry_limit)
        if qty < 1:
            return reject("INSUFFICIENT_BP", symbol, f"buying_power=${buying_power:.2f} insufficient to size a position at entry_limit={entry_limit}")

        # Level-integrity check: reject only if live price has already
        # fallen to/through the stop before the order could be placed --
        # meaning the setup broke down in the gap between signal and
        # submission. Price being above entry_limit (the normal case for
        # a resting pullback-to-level order) is NOT a rejection reason.
        try:
            quote_req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quote = data_client.get_stock_latest_quote(quote_req)[symbol]
            live_price = float(quote.ask_price) if quote.ask_price else float(quote.bid_price)
            if not live_price or live_price <= 0:
                raise ValueError(f"no usable live price (ask={quote.ask_price}, bid={quote.bid_price})")
        except Exception as e:
            return reject("LIVE_PRICE_CHECK_FAILED", symbol, f"could not verify live price, rejecting for safety: {e}", http_status=500, log_level="error")

        stop_breach_level = stop_loss * (1 + STALE_SIGNAL_STOP_BUFFER_PCT / 100)
        if live_price <= stop_breach_level:
            return reject(
                "LEVEL_BROKEN", symbol,
                f"live price ${live_price:.4f} has already reached/breached stop ${stop_loss:.4f} "
                f"(entry_limit ${entry_limit:.4f}) — setup invalidated before order could be placed",
            )

        try:
            # OTO: entry + take_profit only. No native stop_loss leg --
            # per the EMA-close design, there is NO resting exchange-side
            # stop while gain is under TRAIL_ACTIVATION_GAIN_PCT. The
            # EMA_EXIT branch above is the only protection until then;
            # once gain crosses the threshold, the position manager loop
            # creates a native STOP order for the first time and the
            # trailing mechanism takes over completely.
            order = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=entry_limit,
                order_class=OrderClass.OTO,
                take_profit=TakeProfitRequest(limit_price=take_profit),
            )
            submitted = trading_client.submit_order(order)
            recent_signals[symbol] = now

            latency_ms = (time.time() - start_time) * 1000
            send_alert(
                f"[ENTRY] {symbol} qty={qty} (equity-sized) limit={entry_limit} "
                f"TP={take_profit} SL(reference only, no resting order)={stop_loss} "
                f"| stop mechanism: EMA-close exit until +{TRAIL_ACTIVATION_GAIN_PCT}% gain, "
                f"then {TRAIL_PERCENT}% trail "
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
























































































































