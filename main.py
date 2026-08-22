import json
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
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
from alpaca.trading.enums import OrderSide, TimeInForce, OrderType, OrderClass, QueryOrderStatus
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from requests.exceptions import Timeout, ConnectionError

# ============================================================
# Timezones
#   NY_TZ  -- the zone the bot REASONS in (entry window, EOD, session date).
#   MEL_TZ -- the zone the operator READS in. Purely for log display.
# Three zones were previously in play unlabelled: ET (window/EOD strings),
# UTC (Alpaca's filled_at, printed raw), and local Melbourne wall time. Every
# log line is now stamped in ET and Melbourne so a log can be cross-referenced
# against either the market session or the operator's clock without arithmetic.
# ============================================================
NY_TZ = ZoneInfo("America/New_York")
MEL_TZ = ZoneInfo("Australia/Melbourne")


# ============================================================
# Logging — persistent rotating file log + console, BOTH formatted.
# Previously the formatter was attached to the file handler only, so the
# bare StreamHandler() fell back to "%(levelname)s:%(name)s:%(message)s" and
# the Railway console log carried NO timestamp on any line at all. The only
# times visible anywhere were the ones baked into message strings.
# ============================================================
class DualTZFormatter(logging.Formatter):
    """Stamps each record in New York time (what the bot gates on) and
    Melbourne time (what the operator is looking at when reviewing)."""

    def formatTime(self, record, datefmt=None):
        et = datetime.fromtimestamp(record.created, NY_TZ)
        mel = datetime.fromtimestamp(record.created, MEL_TZ)
        return f"{et:%Y-%m-%d %H:%M:%S} ET / {mel:%H:%M} Mel"


_fmt = DualTZFormatter("%(asctime)s [%(levelname)s] %(message)s")

log_handler = RotatingFileHandler("bot.log", maxBytes=10 * 1024 * 1024, backupCount=10)
log_handler.setFormatter(_fmt)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(_fmt)

logging.basicConfig(
    level=logging.INFO,
    handlers=[log_handler, stream_handler],
)
logger = logging.getLogger("trading_bot")

app = Flask(__name__)

is_live = os.getenv("LIVE_TRADING", "False") == "True"

# Build identity. Without this there is no way to tell from a log whether the
# code running on Railway is the code in the repo -- which was exactly the
# ambiguity that made the 20 Aug session impossible to diagnose (the log's
# "(exit fill details unavailable)" wording proved the deployed build predated
# the retry fix, but only by accident of a string having changed).
BUILD_TAG = os.getenv("RAILWAY_GIT_COMMIT_SHA", "local")[:8]

logger.info(f"--- BOT STARTED: LIVE_TRADING={is_live} BUILD={BUILD_TAG} ---")

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

# Position sizing — qty is computed server-side from available buying power
# rather than supplied by Pine. Full-size by design; the candle-range /
# stop-distance checks below are the risk control, not position size.
# NOTE: on the current paper account, buying_power reads equal to equity when
# flat (no margin multiplier). Both are logged at entry so a future account
# with 2x/4x buying power would be visible immediately rather than silently
# doubling or quadrupling every position.
EQUITY_FRACTION = float(os.getenv("EQUITY_FRACTION", "0.98"))

# Defense-in-depth: reject any signal whose implied stop distance exceeds
# this, regardless of what Pine computed — full-size sizing means this is
# the only backstop against an outlier candle producing an outsized loss.
MAX_STOP_DISTANCE_PCT = float(os.getenv("MAX_STOP_DISTANCE_PCT", "15.0"))

# Used purely for informational logging (see webhook). Alpaca's own quote on
# the free IEX-only plan proved unreliable on thin small caps (confirmed live
# with TNON: entry $11.51, quote came back $4.55; and RDAC: entry $14.33,
# quote came back $7.84). Entry/stop/take-profit all come from TradingView's
# real-time feed already, and submitting a limit order to Alpaca doesn't
# require accurate quote data on our side. So a large drift here is logged as
# likely-bad-Alpaca-data, never blocks.
MAX_PLAUSIBLE_PRICE_DRIFT_PCT = float(os.getenv("MAX_PLAUSIBLE_PRICE_DRIFT_PCT", "40.0"))

# The SAME unreliable feed backs pos.current_price, which drives the HWM, the
# trail, and both breakeven tiers. A spuriously LOW print is harmless (the HWM
# only ratchets up), but a spuriously HIGH print inflates the HWM and can place
# a sell stop at or above the real market -- either rejected outright, or
# triggered instantly into a market sell. It can also trip the +8% tier and pin
# the stop at entry*1.04 while price is actually sitting at entry. Any
# cycle-to-cycle move larger than this is treated as bad data and skipped.
MAX_TICK_JUMP_PCT = float(os.getenv("MAX_TICK_JUMP_PCT", "15.0"))

# Flat trailing-stop percentage. The stop only ever ratchets UP (native STOP
# order price is replaced, never loosened) toward highest-price-since-entry
# minus this percent. Take-profit (manual target, set in Pine) is the
# primary exit target; this is the fallback if that target is never reached.
TRAIL_PERCENT = float(os.getenv("TRAIL_PERCENT", "15.0"))

# Breakeven guarantee: once the position is up this much, immediately move
# the stop to entry, independent of what the trail calc alone would give.
# The 15% trail doesn't mathematically reach entry until the position is up
# ~17.6% (entry / 0.85), so without this a trade could run up double-digits
# and give the whole move back before anything protects it.
BREAKEVEN_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TRIGGER_PCT", "2.0"))

# Second breakeven tier: a flat floor at breakeven has the same gap problem
# one level up -- once a trade has run to +8%+, it's proven real strength,
# but the floor still sits pinned at exactly entry until the trail reaches
# ~17.6%. This tier locks in a real profit cushion once that strength shows.
BREAKEVEN_TIER2_TRIGGER_PCT = float(os.getenv("BREAKEVEN_TIER2_TRIGGER_PCT", "8.0"))
BREAKEVEN_TIER2_LOCK_PCT = float(os.getenv("BREAKEVEN_TIER2_LOCK_PCT", "4.0"))

# How often the [ORDERS] diagnostic line is emitted while a position is open.
# This exists to answer one question: can the position manager actually SEE
# the bracket's stop-loss leg? The 20 Aug session proved it never moved a stop
# in six trades -- including one that ran +61% -- so the `if symbol in
# open_orders_map` branch is not being entered, and this says why.
ORDERS_DEBUG_INTERVAL_SECONDS = int(os.getenv("ORDERS_DEBUG_INTERVAL_SECONDS", "30"))

# Idempotency & deduplication
order_lock = threading.Lock()
hwm_lock = threading.RLock()
recent_signals = {}
SIGNAL_DEDUPE_WINDOW_SECONDS = 15

# Which Pine strategy/signal produced each pending/open entry. The current
# Pine ("Range Filter + EMA Gate") has a single entry path, so this defaults
# to RF_EMA and is mostly a build-identity marker in the P&L log. It becomes a
# real measurement again the moment a second entry path is added -- at which
# point Pine should send a distinct "signal" value per path.
DEFAULT_STRATEGY_TAG = os.getenv("DEFAULT_STRATEGY_TAG", "RF_EMA")

# Written by the webhook thread on submission, read by the manager thread when
# a position is first observed, so every [WIN]/[LOSS] line can be attributed to
# the setup that generated it.
pending_signal_types = {}

# HWM persistent storage state
HWM_SAVE_INTERVAL_SECONDS = 5
_last_hwm_save = 0.0
_last_saved_hwm_values = {}
global_hwm = {}

# ============================================================
# Reject-reason tally — reset daily at EOD flatten, logged as one
# summary line so you can see the day's rejection breakdown at a
# glance instead of scrolling the raw log.
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
            parts = ", ".join(
                f"{count}x {tag}"
                for tag, count in sorted(reject_reason_counts.items(), key=lambda kv: -kv[1])
            )
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


def has_any_open_exposure():
    """Hard cap of 1 concurrent position/pending entry across ALL symbols."""
    positions = trading_client.get_all_positions()
    if len(positions) > 0:
        return True
    # nested=False pinned explicitly -- see the note in position_manager_loop.
    open_orders = trading_client.get_orders(
        filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=False)
    )
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
            logger.warning(
                f"Transient error replacing order {order_id} (attempt {attempt}/{retries}): {e}. Retrying in {delay}s..."
            )
            time.sleep(delay)


def retry_cancel_order(order_id, retries=3, delay=1.0):
    for attempt in range(1, retries + 1):
        try:
            return trading_client.cancel_order_by_id(order_id)
        except Exception as e:
            if attempt == retries or not is_transient_error(e):
                raise
            logger.warning(
                f"Transient error cancelling order {order_id} (attempt {attempt}/{retries}): {e}. Retrying in {delay}s..."
            )
            time.sleep(delay)


def position_manager_loop():
    global global_hwm
    global_hwm = load_hwm()
    last_equity_log = 0.0
    eod_flatten_triggered_date = None
    last_eod_failure_alert = 0.0
    last_orders_debug = 0.0

    # Tracks currently-open positions so we can detect the moment one closes
    # (TP fill, trailing-stop fill, or EOD flatten) and log a single clean
    # summary line instead of noisy per-minute updates.
    open_positions_tracked = {}  # symbol -> {"qty", "entry_price", "opened_at", "signal"}

    # Last accepted price per symbol, used to reject implausible ticks from
    # Alpaca's thin/unreliable IEX data before they poison the HWM.
    last_seen_price = {}

    # Tracks order IDs a cancel has already been requested for, so the
    # stale-entry cleanup below doesn't repeatedly re-issue cancel requests
    # every 2s loop cycle while Alpaca is still processing the first one.
    cancel_requested_order_ids = set()

    while True:
        try:
            manager_status["last_heartbeat"] = time.time()
            manager_status["is_alive"] = True
            now = time.time()
            now_ny_dt = datetime.now(NY_TZ)

            # EOD Hard Close: Flatten all positions and cancel open orders at 15:55 ET.
            # Keyed off the NEW YORK date, deliberately. The operator is in
            # Melbourne where a single US session spans two local dates
            # (open ~23:30, EOD flatten ~05:55 next day), so keying off local
            # date would fire the rollover mid-session.
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
                    log_reject_summary()
                    # Only mark today's flatten as done on SUCCESS, so a single
                    # transient failure can't silently stop the bot from ever
                    # attempting to flatten again for the rest of the day.
                    eod_flatten_triggered_date = current_date_str
                except Exception as e:
                    now_ts = time.time()
                    # Retry every loop cycle (~2s) until it succeeds -- but
                    # throttle the alert itself to once per 60s so a persistent
                    # outage doesn't spam the log into unreadability.
                    if now_ts - last_eod_failure_alert >= 60:
                        send_alert(f"[CRIT] EOD Flatten failed: {e} — retrying every cycle until it succeeds.")
                        last_eod_failure_alert = now_ts

            if now - last_equity_log >= 3600:
                try:
                    account = trading_client.get_account()
                    equity = float(account.equity)
                    bp = float(account.buying_power)
                    leverage_note = "" if abs(bp - equity) < 1.0 else f" [WARN: BP != equity, ratio {bp / equity:.2f}x]"
                    logger.info(
                        f"ACCOUNT EQUITY UPDATE: Total Equity = ${equity:.2f}, "
                        f"Buying Power = ${bp:.2f}{leverage_note}"
                    )
                    last_equity_log = now
                except Exception as e:
                    logger.warning(f"Failed to log account equity: {e}")

            positions = trading_client.get_all_positions()
            current_symbols = {p.symbol for p in positions}

            # Trade-close detection: anything we were tracking that's no longer
            # an open position just closed -- via the native TP fill, the
            # trailing STOP fill, or EOD flatten.
            for symbol in set(open_positions_tracked.keys()) - current_symbols:
                info = open_positions_tracked.pop(symbol)
                try:
                    # 'after' is the hard fix: only orders submitted after THIS
                    # position was first observed open can possibly be its exit.
                    # A 5-minute safety buffer covers clock/detection lag.
                    lookback_start = info["opened_at"] - timedelta(minutes=5)

                    # Retry the lookup a couple of times with a short delay --
                    # Alpaca's closed-orders endpoint can lag behind a fill that
                    # just happened (confirmed live: BTCT's 594-share stop fill
                    # was real, but a single lookup came back empty).
                    exit_order = None
                    for attempt in range(3):
                        recent_orders = trading_client.get_orders(
                            filter=GetOrdersRequest(
                                status=QueryOrderStatus.CLOSED,
                                symbols=[symbol],
                                limit=10,
                                after=lookback_start,
                                nested=False,
                            )
                        )
                        exit_order = next(
                            (o for o in recent_orders if o.side == OrderSide.SELL and o.filled_avg_price),
                            None,
                        )
                        if exit_order or attempt == 2:
                            break
                        time.sleep(1.5)

                    sig = info.get("signal", "UNKNOWN")

                    if exit_order:
                        exit_price = float(exit_order.filled_avg_price)
                        qty = float(exit_order.filled_qty or info["qty"])
                        pl = (exit_price - info["entry_price"]) * qty
                        pl_pct = ((exit_price / info["entry_price"]) - 1) * 100
                        # filled_at comes back from Alpaca in UTC. Printing it
                        # raw made every exit look ~4 hours out of the entry
                        # window (14:30 UTC read as 14:30 ET when it was
                        # actually 10:30 ET), which is what made the 20 Aug log
                        # unreadable. Convert to ET to match every other
                        # timestamp the bot emits.
                        exit_time = (
                            exit_order.filled_at.astimezone(NY_TZ).strftime("%H:%M:%S")
                            if exit_order.filled_at
                            else "?"
                        )
                        tag = "[WIN]" if pl >= 0 else "[LOSS]"
                        logger.info(
                            f"{tag} {symbol} [{sig}] entry ${info['entry_price']:.4f} -> "
                            f"exit ${exit_price:.4f} @ {exit_time} ET | qty {qty:.0f} | "
                            f"P/L: ${pl:.2f} ({pl_pct:+.2f}%) | exit: {exit_order.type}"
                        )
                    else:
                        logger.info(
                            f"[?] {symbol} [{sig}] closed | qty {info['qty']:.0f} | "
                            f"(exit fill details unavailable after 3 attempts)"
                        )
                except Exception as e:
                    logger.warning(f"Could not log close summary for {symbol}: {e}")

                symbol_error_counts[symbol] = 0
                symbol_alert_cooldown.pop(symbol, None)
                last_seen_price.pop(symbol, None)
                pending_signal_types.pop(symbol, None)
                with hwm_lock:
                    if symbol in global_hwm:
                        del global_hwm[symbol]
                        maybe_save_hwm(global_hwm, force=True)

            # Track newly-opened positions with their true entry fill price AND
            # the moment we first observed them open. The timestamp is what lets
            # close-detection above only ever consider orders from THIS trade.
            #
            # qty and entry_price are refreshed EVERY cycle for symbols already
            # tracked, not just set once -- a large order on a thin symbol can
            # fill via several partials over a few seconds, and polling once
            # locks in an early partial as "the" position size (confirmed live:
            # MMA's real 1210-share position was tracked as qty 376).
            for p in positions:
                if p.symbol not in open_positions_tracked:
                    open_positions_tracked[p.symbol] = {
                        "qty": float(p.qty),
                        "entry_price": float(p.avg_entry_price),
                        "opened_at": datetime.now(timezone.utc),
                        "signal": pending_signal_types.get(p.symbol, "UNKNOWN"),
                    }
                else:
                    open_positions_tracked[p.symbol]["qty"] = float(p.qty)
                    open_positions_tracked[p.symbol]["entry_price"] = float(p.avg_entry_price)

            # nested=False is pinned explicitly here. If this ever resolves to
            # True, Alpaca rolls bracket child orders up UNDER their parent and
            # they vanish from the top-level list entirely -- which would
            # produce exactly the observed symptom of open_orders_map never
            # containing the stop leg, and therefore no stop ever ratcheting.
            # This is the leading hypothesis for the 20 Aug failure, not a
            # confirmed diagnosis; the [ORDERS] line below is what settles it.
            all_open_orders = trading_client.get_orders(
                filter=GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=False)
            )
            open_orders_map = {}
            for o in all_open_orders:
                if o.type == OrderType.STOP:
                    open_orders_map.setdefault(o.symbol, []).append(o)

            # DIAGNOSTIC: while a position is open, periodically dump exactly
            # what the manager can see. If stop_map_keys is empty while
            # position_symbols is not, the stop legs are invisible to this loop
            # and no amount of trail logic will ever fire. Remove once the root
            # cause is confirmed and fixed.
            if positions and (now - last_orders_debug) >= ORDERS_DEBUG_INTERVAL_SECONDS:
                logger.info(
                    f"[ORDERS] visible="
                    f"{[(o.symbol, str(o.type), str(o.status), str(o.stop_price)) for o in all_open_orders]} "
                    f"| stop_map_keys={list(open_orders_map.keys())} "
                    f"| position_symbols={[p.symbol for p in positions]}"
                )
                last_orders_debug = now

            # Prune the cancel-requested tracking set to only IDs still open.
            open_order_ids_now = {o.id for o in all_open_orders}
            cancel_requested_order_ids &= open_order_ids_now

            # Stale entry order cleanup: an unfilled BUY entry that's been
            # sitting too long blocks the concurrency cap from freeing up.
            # Cancel is only ever REQUESTED once per order -- Alpaca processes
            # cancellation asynchronously, so an order can still show as "open"
            # for a cycle or two after the request goes out.
            for o in all_open_orders:
                if o.side == OrderSide.BUY:
                    age_seconds = (datetime.now(timezone.utc) - o.created_at).total_seconds()
                    if age_seconds >= ENTRY_ORDER_TIMEOUT_SECONDS and o.id not in cancel_requested_order_ids:
                        try:
                            retry_cancel_order(o.id)
                            cancel_requested_order_ids.add(o.id)
                            send_alert(
                                f"[TIMEOUT] Requested cancel for stale unfilled entry for {o.symbol} after "
                                f"{age_seconds:.0f}s ({ENTRY_ORDER_TIMEOUT_SECONDS}s timeout) — "
                                f"slot will free once Alpaca confirms the cancel."
                            )
                        except Exception as e:
                            send_alert(f"[CRIT] Failed to cancel stale entry order {o.id} for {o.symbol}: {e}")

            for pos in positions:
                symbol = pos.symbol
                try:
                    current, entry = float(pos.current_price), float(pos.avg_entry_price)

                    # Bad-data guard. pos.current_price comes from the same
                    # feed already proven unreliable on thin small caps. A
                    # spuriously high print here would inflate the HWM and can
                    # place a sell stop at or above the real market, or trip the
                    # +8% tier while price is actually flat. Skip the cycle
                    # rather than act on it.
                    prev = last_seen_price.get(symbol)
                    if prev and abs(current - prev) / prev * 100 > MAX_TICK_JUMP_PCT:
                        logger.info(
                            f"[PRICE_DATA_INFO] {symbol} implausible tick ${prev:.4f} -> ${current:.4f} "
                            f"(> {MAX_TICK_JUMP_PCT}% in one cycle) — skipping cycle, HWM not updated."
                        )
                        continue
                    last_seen_price[symbol] = current

                    with hwm_lock:
                        if symbol not in global_hwm or current > global_hwm[symbol]:
                            global_hwm[symbol] = current
                            maybe_save_hwm(global_hwm, symbol=symbol, current_price=current)
                        hwm_val = global_hwm.get(symbol, current)

                    # Stop management: three floors combine, whichever is
                    # highest wins (the stop only ever ratchets up):
                    #   1) 15% trail: (highest price since entry) x 0.85
                    #   2) Breakeven (+2%): stop >= entry
                    #   3) Tier-2 lock (+8% -> lock +4%)
                    # The native STOP order already exists from entry (it's the
                    # stop_loss leg of the BRACKET submitted in the webhook) --
                    # this loop only ever replaces its price.
                    if symbol in open_orders_map:
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

                        # Final clamp: never place a sell stop at or above the
                        # price we believe the market is at, regardless of which
                        # floor produced it.
                        desired_stop = min(desired_stop, current * 0.995)

                        for order in open_orders_map[symbol]:
                            current_stop = float(order.stop_price)
                            if desired_stop > current_stop:
                                # Alpaca requires whole-cent stop prices for
                                # anything trading >= $1 -- sub-penny is only
                                # valid under $1. An unconditional 4-decimal
                                # round silently broke the trailing stop on
                                # RDAC (~$18): every replace was rejected with
                                # "sub-penny increment does not fulfill minimum
                                # pricing criteria", so the stop stayed frozen.
                                tick = 0.01 if desired_stop >= 1.0 else 0.0001
                                rounded_stop = round(round(desired_stop / tick) * tick, 4)
                                try:
                                    retry_replace_order(order.id, ReplaceOrderRequest(stop_price=rounded_stop))
                                    tag = be_tag or "[TRAIL]"
                                    logger.info(
                                        f"{tag} {symbol} stop ${current_stop:.4f} -> ${rounded_stop:.4f} "
                                        f"(HWM ${hwm_val:.4f}, price ${current:.4f})"
                                    )
                                except Exception as e:
                                    send_alert(f"[CRIT] Failed to move stop for {symbol}: {e}")
                            else:
                                logger.debug(
                                    f"[TRAIL_SKIP] {symbol} desired ${desired_stop:.4f} <= "
                                    f"current ${current_stop:.4f} (HWM ${hwm_val:.4f}, price ${current:.4f})"
                                )
                    else:
                        # DIAGNOSTIC: a position exists but no STOP order is
                        # visible for it. If this appears every cycle of an open
                        # trade, the entire trail/breakeven layer is inert --
                        # which is what the 20 Aug order history showed.
                        logger.info(
                            f"[TRAIL_MISS] {symbol} has an open position but NO stop order is visible "
                            f"to the manager — trail/breakeven cannot run for this trade."
                        )
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


def log_startup_config():
    """Dumps every live risk/gating parameter at boot. Without this you cannot
    tell from a log which settings produced a session's behaviour -- env vars
    on Railway can drift from the defaults in this file, and a strategy tuned
    against one set of numbers is meaningless if a different set was running.

    Also flags the known misalignment between Pine and the backend: the current
    Pine script caps stop distance at 8% of entry, but MAX_STOP_DISTANCE_PCT
    and TRAIL_PERCENT still default to 15. That is deliberate for now -- these
    are strategy decisions and were left alone so this deploy is a pure bug-fix
    deploy. Override via env var when you tune them.
    """
    logger.info(
        f"[CONFIG] window={TRADING_WINDOW_START}-{TRADING_WINDOW_END} ET | "
        f"equity_fraction={EQUITY_FRACTION} | max_stop_dist={MAX_STOP_DISTANCE_PCT}% | "
        f"trail={TRAIL_PERCENT}% | be_tier1={BREAKEVEN_TRIGGER_PCT}% | "
        f"be_tier2={BREAKEVEN_TIER2_TRIGGER_PCT}%->lock {BREAKEVEN_TIER2_LOCK_PCT}% | "
        f"entry_timeout={ENTRY_ORDER_TIMEOUT_SECONDS}s | max_tick_jump={MAX_TICK_JUMP_PCT}% | "
        f"strategy_tag={DEFAULT_STRATEGY_TAG}"
    )
    if TRAIL_PERCENT >= 12.0:
        logger.info(
            f"[CONFIG] NOTE: trail is {TRAIL_PERCENT}% while Pine caps initial stops at ~8%. "
            f"The trail will not bind until roughly +{(1 / (1 - TRAIL_PERCENT / 100) - 1) * 100:.1f}%, "
            f"so the breakeven tiers do all the work below that. Revisit during tuning."
        )


log_startup_config()


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
        "build": BUILD_TAG,
        "heartbeat_age": round(heartbeat_age, 2),
        "positions": positions_count,
        "uptime_hours": round(uptime_hours, 2),
        "now_et": datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "now_melbourne": datetime.now(MEL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
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
        # No symbol available yet (body itself is missing/invalid) — most of
        # these are TradingView's periodic webhook connectivity test pings.
        return reject("NO_JSON", "-", "no/invalid JSON body received", http_status=400)

    if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
        return reject("BAD_SECRET", data.get("symbol", "-"), "invalid webhook secret", http_status=401)

    symbol = str(data.get("symbol", "UNKNOWN")).upper()

    # Which Pine signal fired. Optional -- the current single-path script
    # doesn't need to send it, but if a future version has multiple entry
    # paths it should, so each one can be attributed separately in the P&L log.
    signal_type = str(data.get("signal", DEFAULT_STRATEGY_TAG)).upper()

    try:
        entry_limit = float(data["entry_limit"])
        stop_loss = float(data["stop_loss"])
        take_profit = float(data["take_profit"])
    except (KeyError, ValueError, TypeError) as e:
        return reject("MALFORMED", symbol, f"malformed payload — {e}", http_status=400)

    # Trading window check runs FIRST, right after the payload is parseable —
    # before any level validation. Otherwise a signal that was both outside the
    # window AND had bad levels gets logged only as "levels out of order",
    # hiding the real first-order reason.
    if not within_trading_window():
        now_ny_str = datetime.now(NY_TZ).strftime("%H:%M:%S")
        return reject(
            "WINDOW", symbol,
            f"arrived at {now_ny_str} ET, outside window {TRADING_WINDOW_START}-{TRADING_WINDOW_END} ET "
            f"(signal={signal_type}, entry_limit={entry_limit}, stop_loss={stop_loss}, take_profit={take_profit})",
        )

    # Level ordering — split into distinct sub-checks so the log says exactly
    # which value broke it. take_profit == 0.0 is called out by name since
    # that's the most common real-world cause.
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

    stop_distance_pct = ((entry_limit - stop_loss) / entry_limit) * 100
    if stop_distance_pct > MAX_STOP_DISTANCE_PCT:
        return reject(
            "STOP_TOO_WIDE", symbol,
            f"stop distance {stop_distance_pct:.2f}% exceeds MAX_STOP_DISTANCE_PCT={MAX_STOP_DISTANCE_PCT}%",
        )

    with order_lock:
        now = time.time()
        global recent_signals
        recent_signals = {s: t for s, t in recent_signals.items() if now - t < SIGNAL_DEDUPE_WINDOW_SECONDS}

        if recent_signals.get(symbol) is not None:
            return reject("DUPLICATE", symbol, f"duplicate signal received again within {SIGNAL_DEDUPE_WINDOW_SECONDS}s")

        try:
            if has_any_open_exposure():
                return reject("CONCURRENCY_CAP", symbol, "max concurrent positions reached (cap: 1)")
        except Exception as e:
            return reject(
                "CONCURRENCY_CHECK_FAILED", symbol,
                f"could not verify concurrency cap, rejecting for safety: {e}",
                http_status=500, log_level="error",
            )

        try:
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
            equity = float(account.equity)
        except Exception as e:
            return reject(
                "BUYING_POWER_CHECK_FAILED", symbol,
                f"could not check buying power, rejecting for safety: {e}",
                http_status=500, log_level="error",
            )

        qty = int((buying_power * EQUITY_FRACTION) // entry_limit)
        if qty < 1:
            return reject(
                "INSUFFICIENT_BP", symbol,
                f"buying_power=${buying_power:.2f} insufficient to size a position at entry_limit={entry_limit}",
            )

        # Live-quote check, INFORMATIONAL ONLY -- logged for visibility, never
        # blocks submission. A total fetch FAILURE still blocks, since that's a
        # genuine infrastructure problem rather than a stale-price problem.
        try:
            quote_req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
            quote = data_client.get_stock_latest_quote(quote_req)[symbol]
            live_price = float(quote.ask_price) if quote.ask_price else float(quote.bid_price)
            if not live_price or live_price <= 0:
                raise ValueError(f"no usable live price (ask={quote.ask_price}, bid={quote.bid_price})")
            price_drift_pct = abs(live_price - entry_limit) / entry_limit * 100
            if price_drift_pct > MAX_PLAUSIBLE_PRICE_DRIFT_PCT:
                logger.info(
                    f"[PRICE_DATA_INFO] {symbol} — Alpaca quote ${live_price:.4f} is {price_drift_pct:.1f}% "
                    f"away from entry_limit ${entry_limit:.4f} (likely Alpaca's own stale/thin data, "
                    f"not a real move) — informational only, proceeding with order."
                )
        except Exception as e:
            return reject(
                "LIVE_PRICE_CHECK_FAILED", symbol,
                f"could not fetch any live quote from Alpaca (infrastructure issue, not a price-drift issue), "
                f"rejecting for safety: {e}",
                http_status=500, log_level="error",
            )

        try:
            # BRACKET: entry + take_profit + stop_loss (signal candle's low
            # from Pine). Alpaca links take_profit and stop_loss as OCO
            # siblings natively -- whichever fires, the other is auto-cancelled.
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
            pending_signal_types[symbol] = signal_type

            latency_ms = (time.time() - start_time) * 1000
            leverage_note = "" if abs(buying_power - equity) < 1.0 else f" [WARN: BP/equity {buying_power / equity:.2f}x]"
            send_alert(
                f"[ENTRY] {symbol} [{signal_type}] qty={qty} limit={entry_limit} "
                f"TP={take_profit} SL={stop_loss} (stop dist {stop_distance_pct:.2f}%, trailing {TRAIL_PERCENT}%) "
                f"| equity=${equity:.2f} bp=${buying_power:.2f}{leverage_note} "
                f"| Order ID: {submitted.id} | Client Order ID: {submitted.client_order_id} | "
                f"Status: {submitted.status} | Latency: {latency_ms:.1f}ms"
            )
            logger.info(
                f"Order submitted: {submitted.id} (Client ID: {submitted.client_order_id}) "
                f"for {symbol} with status {submitted.status}"
            )
        except Exception as e:
            send_alert(f"[CRIT] Failed to submit entry order for {symbol}: {e}")
            return "Order submission failed", 500

    return "Success", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
























































































































