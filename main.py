"""
Trading bot backend — TradingView -> Flask -> Alpaca.

ARCHITECTURE
------------
TradingView decides EVERYTHING. Alpaca only executes. This file is a
translator between the two, and deliberately makes no price judgements
of its own.

That division exists because Alpaca's free IEX data proved unreliable on
thin small caps (confirmed live: TNON entry $11.51 vs quote $4.55; RDAC
$14.33 vs $7.84). Any logic here that read a price would be reading that
feed. So the trail, the exit level and the high-water mark all now live
in Pine, computed on TradingView's data, and arrive as instructions.

Three inbound message types:

  entry        -> submit a BRACKET order (entry + TP + native stop)
  stop_update  -> replace the native stop leg's price (ratchet up only)
  exit         -> cancel resting legs, then close the position at market

MULTI-STRATEGY (added Sep 2026)
-------------------------------
More than one Pine script now feeds this endpoint. Each entry payload
carries a "signal" tag naming the strategy. That tag is used for:

  * per-strategy stop-distance caps (STRATEGY_MAX_STOP_PCT), because a
    stop taken from a 5-minute candle's low is structurally wider than one
    taken from a 1-minute candle's, and a single global cap cannot be
    right for both;
  * per-strategy reject tallies and win/loss stats, so two strategies
    sharing one account can still be told apart in the log;
  * strategy-scoped duplicate suppression.

Strategies still share ONE account and ONE concurrency cap. Two strategies
firing on the same morning compete for the same slot — see
MAX_CONCURRENT_POSITIONS below before assuming their results are
comparable.

WHAT THIS FILE DOES NOT DO
--------------------------
No trailing stop calculation. No breakeven tiers. No high-water mark
tracking. No live-quote sanity check. All of it lives in Pine. The
position manager thread handles housekeeping only: heartbeat, EOD
flatten, stale entry cleanup, naked-position watchdog, and exit/P&L
logging.
"""

import json
import logging
import os
import signal
import sys
import threading
import time
from collections import defaultdict
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
from requests.exceptions import Timeout, ConnectionError

# ============================================================
# Timezones
#   NY_TZ  -- the zone the bot REASONS in (entry window, EOD, session date)
#   MEL_TZ -- the zone the operator READS in (display only)
# ============================================================
NY_TZ = ZoneInfo("America/New_York")
MEL_TZ = ZoneInfo("Australia/Melbourne")


class DualTZFormatter(logging.Formatter):
    """Stamps each record in New York time (what the bot gates on) and
    Melbourne time (what the operator sees when reviewing). Previously the
    formatter was attached to the file handler only, so the Railway console
    log carried no timestamp on any line at all."""

    def formatTime(self, record, datefmt=None):
        et = datetime.fromtimestamp(record.created, NY_TZ)
        mel = datetime.fromtimestamp(record.created, MEL_TZ)
        return f"{et:%Y-%m-%d %H:%M:%S} ET / {mel:%H:%M} Mel"


_fmt = DualTZFormatter("%(asctime)s [%(levelname)s] %(message)s")

log_handler = RotatingFileHandler("bot.log", maxBytes=10 * 1024 * 1024, backupCount=10)
log_handler.setFormatter(_fmt)

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(_fmt)

logging.basicConfig(level=logging.INFO, handlers=[log_handler, stream_handler])
logger = logging.getLogger("trading_bot")

app = Flask(__name__)

is_live = os.getenv("LIVE_TRADING", "False") == "True"

# Build identity. Without this there is no way to tell from a log whether
# the code running on Railway is the code in the repo -- exactly the
# ambiguity that made the 20 Aug session impossible to diagnose.
BUILD_TAG = os.getenv("RAILWAY_GIT_COMMIT_SHA", "local")[:8]

logger.info(f"--- BOT STARTED: LIVE_TRADING={is_live} BUILD={BUILD_TAG} ---")

trading_client = TradingClient(
    os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"), paper=not is_live
)

bot_start_time = time.time()
manager_status = {"last_heartbeat": time.time(), "is_alive": True}
symbol_error_counts = {}
symbol_alert_cooldown = {}
ERROR_ALERT_INTERVAL = 10

# An unfilled entry blocks the concurrency cap from freeing up.
ENTRY_ORDER_TIMEOUT_SECONDS = int(os.getenv("ENTRY_ORDER_TIMEOUT_SECONDS", "300"))

# Entry window (ET). Applies to ENTRIES ONLY -- stop_update and exit are
# never time-gated, or a position opened at 11:30 would become unmanaged.
TRADING_WINDOW_START = os.getenv("TRADING_WINDOW_START", "09:35")
TRADING_WINDOW_END = os.getenv("TRADING_WINDOW_END", "11:35")

# Concurrency. ONE by default and it should stay there unless there is a
# deliberate reason to change it.
#
# Raising this does NOT simply let two strategies run side by side on equal
# terms. Sizing below divides the allocation by the cap, so a cap of 2
# halves every position -- including the days when only one strategy fires
# at all. That is a real cost paid on every trade to buy comparability on
# the few days both fire. Decide which you want before touching it.
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))

# Position sizing. Full-size by design when the cap is 1. On the current
# paper account buying_power equals equity when flat (no margin
# multiplier); both are logged at entry so a change would be visible
# immediately rather than silently doubling every position.
EQUITY_FRACTION = float(os.getenv("EQUITY_FRACTION", "0.98"))

# Defense-in-depth on stop width. Pine's own max-stop input was removed,
# so this is the ONLY guard against an outlier bar producing an oversized
# loss at full size. Do not remove it.
#
# MAX_STOP_DISTANCE_PCT is the fallback. STRATEGY_MAX_STOP_PCT overrides it
# per strategy tag, as JSON:
#
#   STRATEGY_MAX_STOP_PCT={"RF_EXEC":15.0,"VWAP_X_LOWTEST":9.0,"VWAP_X_VOLSHIFT":9.0}
#
# The 5-minute crossover strategy takes its stop from a 5-minute candle's
# low, which sits materially further from the close than a 1-minute
# candle's does. Same account, same equity fraction, wider stop, bigger
# dollar loss. One global number cannot be correct for both.
MAX_STOP_DISTANCE_PCT = float(os.getenv("MAX_STOP_DISTANCE_PCT", "15.0"))


def _parse_strategy_stop_caps():
    raw = os.getenv("STRATEGY_MAX_STOP_PCT", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        out = {str(k).upper(): float(v) for k, v in parsed.items()}
        return out
    except Exception as e:
        logger.error(
            f"[CONFIG] STRATEGY_MAX_STOP_PCT could not be parsed ({e}). "
            f"Falling back to MAX_STOP_DISTANCE_PCT={MAX_STOP_DISTANCE_PCT}% for every strategy."
        )
        return {}


STRATEGY_MAX_STOP_PCT = _parse_strategy_stop_caps()


def max_stop_pct_for(strategy):
    """Per-strategy cap, falling back to the global one."""
    return STRATEGY_MAX_STOP_PCT.get(strategy.upper(), MAX_STOP_DISTANCE_PCT)


# Diagnostic cadence while a position is open.
ORDERS_DEBUG_INTERVAL_SECONDS = int(os.getenv("ORDERS_DEBUG_INTERVAL_SECONDS", "30"))

DEFAULT_STRATEGY_TAG = os.getenv("DEFAULT_STRATEGY_TAG", "RF_EXEC")

order_lock = threading.Lock()

# Keyed by (symbol, strategy). Two different strategies firing on the same
# symbol within the window are two different decisions, not a duplicate --
# the concurrency cap is what stops both being taken, and it gives a
# meaningful reject reason. Suppressing one as a "duplicate" would hide it.
recent_signals = {}
SIGNAL_DEDUPE_WINDOW_SECONDS = 15

pending_signal_types = {}

reject_tally_lock = threading.Lock()
reject_reason_counts = {}

# Per-strategy daily results, so two strategies sharing one account can
# still be told apart at EOD.
strategy_stats_lock = threading.Lock()
strategy_stats = defaultdict(lambda: {"entries": 0, "wins": 0, "losses": 0, "pl": 0.0})

# Symbols whose bracket legs were cancelled but whose close then FAILED.
# That is the one genuinely dangerous state this process can create: an open
# position with no protective stop. The manager thread retries these every
# cycle until the position is actually gone, the same pattern the EOD flatten
# uses. Nothing here should ever depend on a single API call succeeding.
force_close_lock = threading.Lock()
force_close_symbols = set()

# symbol -> order id of the bracket's STOP leg, captured from the submit
# response at entry time.
#
# WHY THIS EXISTS: the open-orders LIST endpoint does not reliably return
# both OCO siblings once the parent entry has filled. Confirmed live across
# four trades -- while the entry was unfilled both legs showed as HELD and
# nested, then the moment the entry left the open list only the take-profit
# LIMIT came back and the STOP disappeared. The stop was never gone: DAIC's
# and PMI's later FILLED, and PRZO's was auto-cancelled by Alpaca when we
# closed the position. It was purely invisible to the query, which is why
# every stop_update came back NO_STOP_ORDER and the trail never once ran.
#
# Looking the leg up by its own id sidesteps the list endpoint entirely.
stop_leg_lock = threading.Lock()
stop_leg_ids = {}


def remember_stop_leg(symbol, order):
    """Pulls the STOP leg's id out of a submit/replace response."""
    for leg in (getattr(order, "legs", None) or []):
        if leg.type == OrderType.STOP:
            with stop_leg_lock:
                stop_leg_ids[symbol] = leg.id
            logger.info(f"[LEG] {symbol} stop leg id captured: {leg.id}")
            return leg.id
    return None


def forget_stop_leg(symbol):
    with stop_leg_lock:
        stop_leg_ids.pop(symbol, None)


def reject(tag, symbol, message, http_status=200, log_level="info", strategy=None):
    """Central reject helper. Every rejection MUST go through this so the
    reason is tagged distinctly in the log and tallied for the daily
    summary. `tag` is a short stable code, not a sentence.

    `strategy` is optional and only known once a payload has been parsed
    far enough to read it -- malformed-payload rejects will not have it."""
    key = f"{tag}|{strategy.upper()}" if strategy else tag
    with reject_tally_lock:
        reject_reason_counts[key] = reject_reason_counts.get(key, 0) + 1
    prefix = f"[REJECT:{tag}]" if not strategy else f"[REJECT:{tag}][{strategy.upper()}]"
    full_message = f"{prefix} {symbol} — {message}"
    if log_level == "error":
        logger.error(full_message)
    elif log_level == "debug":
        logger.debug(full_message)
    else:
        logger.info(f"ALERT: {full_message}")
    return message, http_status


def log_reject_summary():
    with reject_tally_lock:
        if not reject_reason_counts:
            logger.info("[SUMMARY] No rejected signals today.")
        else:
            parts = ", ".join(
                f"{count}x {key}"
                for key, count in sorted(reject_reason_counts.items(), key=lambda kv: -kv[1])
            )
            logger.info(f"[SUMMARY] Reject reasons today: {parts}")
        reject_reason_counts.clear()


def log_strategy_summary():
    """Per-strategy results for the session. With two strategies sharing one
    account this is the only place the split is visible."""
    with strategy_stats_lock:
        if not strategy_stats:
            logger.info("[SUMMARY] No entries taken today.")
        else:
            for tag, s in sorted(strategy_stats.items()):
                closed = s["wins"] + s["losses"]
                wr = (s["wins"] / closed * 100) if closed else 0.0
                logger.info(
                    f"[SUMMARY] {tag}: entries={s['entries']} closed={closed} "
                    f"W/L={s['wins']}/{s['losses']} ({wr:.0f}%) P/L=${s['pl']:.2f}"
                )
        strategy_stats.clear()


def is_transient_error(e):
    if isinstance(e, (Timeout, ConnectionError)):
        return True
    err_str = str(e)
    transient = ["429", "500", "502", "503", "504", "timeout", "reset", "temporarily unavailable"]
    return any(i in err_str for i in transient)


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


def round_tick(price):
    """Alpaca requires whole-cent prices at or above $1; sub-penny is only
    valid below it. An unconditional 4-decimal round silently broke the
    trailing stop on RDAC (~$18) -- every replace was rejected for a
    sub-penny increment, so the stop stayed frozen while the position ran."""
    tick = 0.01 if price >= 1.0 else 0.0001
    return round(round(price / tick) * tick, 4)


def get_open_orders(symbol=None, nested=True):
    """Open orders, nested by default.

    CONFIRMED LIVE 24 Aug: with nested=False the query returned ONLY the
    take-profit LIMIT leg -- the stop leg was absent, yet demonstrably alive
    (both trades that day exited with `exit: stop`). Alpaca returns the
    bracket's OCO siblings as LEGS of one another rather than as separate
    top-level orders, so a flat query silently loses one of them.

    That is the whole reason the trailing stop has never once ratcheted in
    production. It was never a logic bug -- the manager simply could not
    see the order it was trying to move."""
    if symbol:
        f = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], nested=nested)
    else:
        f = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=nested)
    return trading_client.get_orders(filter=f)


def flatten_orders(orders):
    """Walks an order list and yields every order AND every nested leg.

    Alpaca nests recursively (a bracket parent holds legs, and after the
    entry fills those legs hold each other), so this recurses rather than
    checking one level."""
    seen = set()
    out = []

    def walk(o):
        oid = getattr(o, "id", None)
        if oid is not None and oid in seen:
            return
        if oid is not None:
            seen.add(oid)
        out.append(o)
        for leg in (getattr(o, "legs", None) or []):
            walk(leg)

    for o in orders:
        walk(o)
    return out


DEAD_STATUSES = ("filled", "canceled", "cancelled", "expired", "rejected", "done_for_day", "replaced")


def _is_live(order):
    return str(getattr(order, "status", "")).lower().split(".")[-1] not in DEAD_STATUSES


def get_stop_orders(symbol):
    """The live STOP order(s) for a symbol.

    Tries the remembered leg id FIRST, because the list endpoint drops the
    stop sibling once the entry fills. Falls back to scanning the list only
    if there is no remembered id or the direct lookup comes back dead."""
    with stop_leg_lock:
        leg_id = stop_leg_ids.get(symbol)

    if leg_id:
        try:
            o = trading_client.get_order_by_id(leg_id)
            if o is not None and _is_live(o) and getattr(o, "stop_price", None) is not None:
                return [o]
            logger.info(
                f"[LEG] {symbol} remembered stop leg {leg_id} is no longer live "
                f"(status={getattr(o, 'status', '?')}); falling back to list scan"
            )
            forget_stop_leg(symbol)
        except Exception as e:
            logger.warning(f"[LEG] {symbol} direct lookup of {leg_id} failed: {e}; falling back to list scan")

    stops = []
    for o in flatten_orders(get_open_orders(symbol)):
        if o.type == OrderType.STOP and getattr(o, "stop_price", None) is not None and _is_live(o):
            stops.append(o)
    if stops:
        with stop_leg_lock:
            stop_leg_ids[symbol] = stops[0].id
    return stops


def open_exposure_count():
    """How many slots are currently consumed, across ALL symbols and ALL
    strategies: open positions plus resting unfilled entries.

    Counts distinct symbols rather than raw orders. A bracket's entry leg
    and its position never coexist for the same symbol in practice, but
    counting symbols makes the cap mean what it says regardless."""
    symbols = {p.symbol for p in trading_client.get_all_positions()}
    for o in flatten_orders(get_open_orders()):
        if o.side == OrderSide.BUY:
            symbols.add(o.symbol)
    return len(symbols)


def get_position(symbol):
    for p in trading_client.get_all_positions():
        if p.symbol == symbol:
            return p
    return None


def retry_replace_order(order_id, replace_request, retries=3, delay=1.0):
    for attempt in range(1, retries + 1):
        try:
            return trading_client.replace_order_by_id(order_id, replace_request)
        except Exception as e:
            if attempt == retries or not is_transient_error(e):
                raise
            logger.warning(f"Transient error replacing {order_id} ({attempt}/{retries}): {e}")
            time.sleep(delay)


def retry_cancel_order(order_id, retries=3, delay=1.0):
    for attempt in range(1, retries + 1):
        try:
            return trading_client.cancel_order_by_id(order_id)
        except Exception as e:
            if attempt == retries or not is_transient_error(e):
                raise
            logger.warning(f"Transient error cancelling {order_id} ({attempt}/{retries}): {e}")
            time.sleep(delay)


def cancel_resting_legs(symbol):
    """Cancels every open order for a symbol before a manual close.

    This ordering is the AMIX fix. Calling close_position() while a bracket
    sibling still rests at the exchange leaves a stray order that can fire
    later against a position that no longer exists. Cancel first, then
    close."""
    cancelled = 0
    try:
        for o in flatten_orders(get_open_orders(symbol)):
            try:
                retry_cancel_order(o.id)
                cancelled += 1
            except Exception as e:
                logger.warning(f"Could not cancel {o.id} for {symbol}: {e}")
    except Exception as e:
        logger.warning(f"Could not list open orders for {symbol}: {e}")
    return cancelled


def verify_legs_cancelled(symbol, timeout_s=3.0, poll_s=0.4):
    """Polls until no open SELL order or leg remains for the symbol.

    Alpaca processes cancellation ASYNCHRONOUSLY, so cancel_resting_legs()
    returning does not mean the legs are gone -- it means the requests were
    accepted. Closing in that window can leave a stray leg alive against a
    position that no longer exists (the AMIX failure).

    Bounded on purpose: waiting indefinitely would be worse than a stray
    order, because the position stays open the whole time. Returns
    (clear, remaining) and the caller proceeds either way."""
    deadline = time.time() + timeout_s
    remaining = []
    while True:
        try:
            remaining = [o for o in flatten_orders(get_open_orders(symbol)) if o.side == OrderSide.SELL]
        except Exception as e:
            logger.warning(f"Could not verify leg cancellation for {symbol}: {e}")
            return False, []
        if not remaining:
            return True, []
        if time.time() >= deadline:
            return False, remaining
        time.sleep(poll_s)


def position_gone(symbol):
    try:
        return get_position(symbol) is None
    except Exception:
        return False


def close_position_with_retry(symbol, reason, attempts=3, delay=1.0):
    """Closes a position, retrying on failure.

    A close that fails AFTER the legs were cancelled leaves an unprotected
    position, so a single attempt is not good enough. If every attempt fails
    the symbol is registered for the manager thread to keep retrying rather
    than being abandoned with a log line."""
    for attempt in range(1, attempts + 1):
        try:
            trading_client.close_position(symbol)
            return True
        except Exception as e:
            # The stop leg may have filled during the verify window. That is
            # a normal outcome, not a failure -- the position is flat either
            # way, which is what was wanted.
            if position_gone(symbol):
                logger.info(f"[EXIT] {symbol} — already flat before close completed ({reason})")
                return True
            if attempt == attempts:
                with force_close_lock:
                    force_close_symbols.add(symbol)
                send_alert(
                    f"[CRIT] Failed to close {symbol} after {attempts} attempts ({reason}): {e} — "
                    f"POSITION MAY BE UNPROTECTED (legs already cancelled). "
                    f"Registered for retry every cycle until flat."
                )
                return False
            logger.warning(f"Close attempt {attempt}/{attempts} failed for {symbol}: {e}. Retrying...")
            time.sleep(delay)
    return False


# ============================================================
# Position manager — housekeeping only.
#
# It does NOT compute a trail, a breakeven, or a high-water mark any
# more. Those live in Pine. What remains: heartbeat, EOD flatten, stale
# entry cleanup, naked-position watchdog, exit/P&L logging, and the
# [ORDERS] diagnostic.
# ============================================================
def position_manager_loop():
    last_equity_log = 0.0
    eod_flatten_triggered_date = None
    last_eod_failure_alert = 0.0
    last_orders_debug = 0.0

    open_positions_tracked = {}
    cancel_requested_order_ids = set()
    naked_streak = {}

    while True:
        try:
            manager_status["last_heartbeat"] = time.time()
            manager_status["is_alive"] = True
            now = time.time()
            now_ny_dt = datetime.now(NY_TZ)

            # EOD flatten at 15:55 ET. Keyed off the NEW YORK date: the
            # operator is in Melbourne where one US session spans two local
            # dates (open ~23:30, flatten ~05:55 next day), so a local-date
            # key would roll over mid-session.
            current_date_str = now_ny_dt.strftime("%Y-%m-%d")
            eod_target_time = datetime.strptime("15:55", "%H:%M").time()

            if now_ny_dt.time() >= eod_target_time and eod_flatten_triggered_date != current_date_str:
                send_alert("[EOD] Reached 15:55 ET. Flattening all positions and cancelling open orders.")
                try:
                    trading_client.close_all_positions(cancel_orders=True)
                    send_alert("[EOD] Successfully closed all positions.")
                    # Give close detection a cycle to log the final exits
                    # before the per-strategy tally is printed and cleared.
                    time.sleep(3)
                    log_reject_summary()
                    log_strategy_summary()
                    # Only mark done on SUCCESS. Setting this unconditionally
                    # would let one transient failure silently stop the bot
                    # from ever retrying for the rest of the day.
                    eod_flatten_triggered_date = current_date_str
                except Exception as e:
                    if time.time() - last_eod_failure_alert >= 60:
                        send_alert(f"[CRIT] EOD Flatten failed: {e} — retrying every cycle until it succeeds.")
                        last_eod_failure_alert = time.time()

            if now - last_equity_log >= 3600:
                try:
                    account = trading_client.get_account()
                    equity = float(account.equity)
                    bp = float(account.buying_power)
                    note = "" if abs(bp - equity) < 1.0 else f" [WARN: BP/equity {bp / equity:.2f}x]"
                    logger.info(f"ACCOUNT EQUITY: ${equity:.2f} | Buying Power: ${bp:.2f}{note}")
                    last_equity_log = now
                except Exception as e:
                    logger.warning(f"Failed to log account equity: {e}")

            # Retry any close that failed earlier. Highest priority in the
            # cycle: these are positions whose protective stop was already
            # cancelled, so every second they stay open is unhedged.
            with force_close_lock:
                stuck = list(force_close_symbols)
            for sym in stuck:
                if position_gone(sym):
                    with force_close_lock:
                        force_close_symbols.discard(sym)
                    send_alert(f"[EXIT] {sym} — position confirmed flat, cleared from force-close retry.")
                    continue
                try:
                    cancel_resting_legs(sym)
                    trading_client.close_position(sym)
                    send_alert(f"[EXIT] {sym} — force-close retry submitted.")
                except Exception as e:
                    logger.warning(f"Force-close retry failed for {sym}: {e}")

            positions = trading_client.get_all_positions()
            current_symbols = {p.symbol for p in positions}

            # Close detection. Anything tracked that is no longer open just
            # closed -- via TP, native stop, our own exit, or EOD flatten.
            for symbol in set(open_positions_tracked.keys()) - current_symbols:
                info = open_positions_tracked.pop(symbol)
                sig = info.get("signal", DEFAULT_STRATEGY_TAG)
                try:
                    # 'after' is the hard fix: only orders submitted after THIS
                    # position was first observed can possibly be its exit.
                    # Without it, a stale closed order from an earlier session
                    # got mistaken for this trade's exit (EHGO logged a bogus
                    # +39.3% win that matched no real order).
                    lookback_start = info["opened_at"] - timedelta(minutes=5)

                    # Retry: Alpaca's closed-orders endpoint can lag a fill that
                    # just happened. A single lookup missed BTCT's real 594-share
                    # stop fill and logged "unavailable" instead of the price.
                    exit_order = None
                    for attempt in range(3):
                        recent = trading_client.get_orders(
                            filter=GetOrdersRequest(
                                status=QueryOrderStatus.CLOSED,
                                symbols=[symbol],
                                limit=10,
                                after=lookback_start,
                                nested=False,
                            )
                        )
                        exit_order = next(
                            (o for o in recent if o.side == OrderSide.SELL and o.filled_avg_price), None
                        )
                        if exit_order or attempt == 2:
                            break
                        time.sleep(1.5)

                    if exit_order:
                        exit_price = float(exit_order.filled_avg_price)
                        qty = float(exit_order.filled_qty or info["qty"])
                        pl = (exit_price - info["entry_price"]) * qty
                        pl_pct = ((exit_price / info["entry_price"]) - 1) * 100
                        # filled_at is UTC. Printing it raw made every exit look
                        # ~4 hours outside the entry window.
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
                        with strategy_stats_lock:
                            s = strategy_stats[sig]
                            s["pl"] += pl
                            if pl >= 0:
                                s["wins"] += 1
                            else:
                                s["losses"] += 1
                    else:
                        logger.info(
                            f"[?] {symbol} [{sig}] closed | qty {info['qty']:.0f} | "
                            f"(exit fill details unavailable after 3 attempts)"
                        )
                except Exception as e:
                    logger.warning(f"Could not log close summary for {symbol}: {e}")

                symbol_error_counts[symbol] = 0
                symbol_alert_cooldown.pop(symbol, None)
                pending_signal_types.pop(symbol, None)
                forget_stop_leg(symbol)

            # qty and entry_price are refreshed EVERY cycle, not just on first
            # sight. A large order on a thin symbol fills via several partials
            # over seconds; polling once locked in an early partial as "the"
            # size (MMA's real 1210 shares were tracked as 376).
            for p in positions:
                if p.symbol not in open_positions_tracked:
                    open_positions_tracked[p.symbol] = {
                        "qty": float(p.qty),
                        "entry_price": float(p.avg_entry_price),
                        "opened_at": datetime.now(timezone.utc),
                        "signal": pending_signal_types.get(p.symbol, DEFAULT_STRATEGY_TAG),
                    }
                else:
                    open_positions_tracked[p.symbol]["qty"] = float(p.qty)
                    open_positions_tracked[p.symbol]["entry_price"] = float(p.avg_entry_price)

            all_open_orders = flatten_orders(get_open_orders())

            # Naked-position watchdog. A position with no live stop is the
            # one state that must never pass silently. Two consecutive misses
            # rather than one, so a momentary lookup blip is not alarming.
            for p in positions:
                try:
                    live_stops = get_stop_orders(p.symbol)
                except Exception:
                    live_stops = []
                if live_stops:
                    naked_streak.pop(p.symbol, None)
                else:
                    naked_streak[p.symbol] = naked_streak.get(p.symbol, 0) + 1
                    if naked_streak[p.symbol] == 2:
                        send_alert(
                            f"[CRIT] {p.symbol} has an OPEN POSITION and NO LIVE STOP ORDER "
                            f"(qty {p.qty}) — position is unprotected."
                        )
            for sym in list(naked_streak.keys()):
                if sym not in current_symbols:
                    naked_streak.pop(sym, None)

            # DIAGNOSTIC: while a position is open, dump what the manager can
            # actually see. On 20 Aug the stop legs were invisible to this
            # loop across six trades and nothing said so.
            if positions and (now - last_orders_debug) >= ORDERS_DEBUG_INTERVAL_SECONDS:
                stop_orders = [o for o in all_open_orders if o.type == OrderType.STOP]
                logger.info(
                    f"[ORDERS] visible={[(o.symbol, str(o.type).split('.')[-1], str(o.status).split('.')[-1], str(o.stop_price)) for o in all_open_orders]} "
                    f"| stop_legs={[(o.symbol, str(o.stop_price)) for o in stop_orders]} "
                    f"| positions={[p.symbol for p in positions]} "
                    f"| remembered_legs={dict(stop_leg_ids)}"
                )
                last_orders_debug = now

            cancel_requested_order_ids &= {o.id for o in all_open_orders}

            # Stale entry cleanup. Cancel is REQUESTED once per order --
            # Alpaca processes cancellation asynchronously, so an order can
            # still show open for a cycle or two afterwards; re-issuing every
            # cycle just spams false [CRIT] alerts.
            for o in all_open_orders:
                if o.side == OrderSide.BUY:
                    age = (datetime.now(timezone.utc) - o.created_at).total_seconds()
                    if age >= ENTRY_ORDER_TIMEOUT_SECONDS and o.id not in cancel_requested_order_ids:
                        try:
                            retry_cancel_order(o.id)
                            cancel_requested_order_ids.add(o.id)
                            # The strategy tag was staged at submit time and is
                            # only cleared on close detection. An entry that
                            # never fills never closes, so without this the tag
                            # leaks and mislabels the NEXT trade on that symbol.
                            pending_signal_types.pop(o.symbol, None)
                            send_alert(
                                f"[TIMEOUT] Requested cancel for stale unfilled entry for {o.symbol} "
                                f"after {age:.0f}s — slot frees once Alpaca confirms."
                            )
                        except Exception as e:
                            send_alert(f"[CRIT] Failed to cancel stale entry {o.id} for {o.symbol}: {e}")

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
    logger.info("Shutdown signal received.")
    sys.exit(0)


signal.signal(signal.SIGINT, handle_shutdown)
signal.signal(signal.SIGTERM, handle_shutdown)


def log_startup_config():
    """Every live risk parameter at boot. Railway env vars drift from the
    defaults in this file, and a strategy tuned against numbers that were
    not the ones running is worthless."""
    logger.info(
        f"[CONFIG] entry_window={TRADING_WINDOW_START}-{TRADING_WINDOW_END} ET | "
        f"equity_fraction={EQUITY_FRACTION} | max_concurrent={MAX_CONCURRENT_POSITIONS} | "
        f"entry_timeout={ENTRY_ORDER_TIMEOUT_SECONDS}s | default_strategy_tag={DEFAULT_STRATEGY_TAG}"
    )
    if STRATEGY_MAX_STOP_PCT:
        caps = ", ".join(f"{k}={v}%" for k, v in sorted(STRATEGY_MAX_STOP_PCT.items()))
        logger.info(f"[CONFIG] stop caps: {caps} | fallback={MAX_STOP_DISTANCE_PCT}%")
    else:
        logger.info(
            f"[CONFIG] stop cap: {MAX_STOP_DISTANCE_PCT}% for ALL strategies "
            f"(STRATEGY_MAX_STOP_PCT not set — a 5-minute-candle stop and a "
            f"1-minute-candle stop are being held to the same limit)"
        )
    logger.info(
        "[CONFIG] Trail, exit level and high-water mark are owned by Pine. "
        "This process computes no price levels of its own."
    )


log_startup_config()


@app.route("/health", methods=["GET"])
def health():
    alive = manager_status["is_alive"]
    heartbeat_age = time.time() - manager_status["last_heartbeat"]
    is_healthy = alive and heartbeat_age < 30
    try:
        positions_count = len(trading_client.get_all_positions())
    except Exception:
        positions_count = 0
    payload = {
        "alive": alive,
        "build": BUILD_TAG,
        "heartbeat_age": round(heartbeat_age, 2),
        "positions": positions_count,
        "uptime_hours": round((time.time() - bot_start_time) / 3600.0, 2),
        "now_et": datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "now_melbourne": datetime.now(MEL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }
    return (payload, 200) if is_healthy else (payload, 500)


@app.route("/reject-summary", methods=["GET"])
def reject_summary():
    with reject_tally_lock:
        return dict(reject_reason_counts), 200


@app.route("/strategy-summary", methods=["GET"])
def strategy_summary():
    """Live per-strategy tally without waiting for EOD. Read-only — it does
    NOT clear the counters, unlike the EOD summary."""
    with strategy_stats_lock:
        return {k: dict(v) for k, v in strategy_stats.items()}, 200


# ============================================================
# ENTRY
# ============================================================
def handle_entry(data, symbol, signal_type, start_time):
    try:
        entry_limit = float(data["entry_limit"])
        stop_loss = float(data["stop_loss"])
        take_profit = float(data["take_profit"])
    except (KeyError, ValueError, TypeError) as e:
        return reject("MALFORMED", symbol, f"malformed entry payload — {e}",
                      http_status=400, strategy=signal_type)

    # Window first, so a signal that is BOTH out of window and badly formed
    # logs the real first-order reason instead of hiding it.
    if not within_trading_window():
        now_ny_str = datetime.now(NY_TZ).strftime("%H:%M:%S")
        return reject(
            "WINDOW", symbol,
            f"arrived at {now_ny_str} ET, outside {TRADING_WINDOW_START}-{TRADING_WINDOW_END} ET "
            f"(entry={entry_limit}, sl={stop_loss}, tp={take_profit})",
            strategy=signal_type,
        )

    if not stop_loss > 0:
        return reject("SL_INVALID", symbol, f"stop_loss must be positive (got {stop_loss})",
                      strategy=signal_type)
    if not stop_loss < entry_limit:
        return reject("SL_GE_ENTRY", symbol, f"stop_loss ({stop_loss}) must be below entry ({entry_limit})",
                      strategy=signal_type)
    if take_profit == 0.0:
        return reject("TP_NOT_SET", symbol, "take_profit is 0.0 — target not set on this ticker's chart",
                      strategy=signal_type)
    if not entry_limit < take_profit:
        return reject("TP_LE_ENTRY", symbol, f"entry ({entry_limit}) must be below take_profit ({take_profit})",
                      strategy=signal_type)

    stop_distance_pct = ((entry_limit - stop_loss) / entry_limit) * 100
    cap = max_stop_pct_for(signal_type)
    if stop_distance_pct > cap:
        return reject(
            "STOP_TOO_WIDE", symbol,
            f"stop distance {stop_distance_pct:.2f}% exceeds cap {cap}% for this strategy",
            strategy=signal_type,
        )

    with order_lock:
        now = time.time()
        global recent_signals
        recent_signals = {k: t for k, t in recent_signals.items() if now - t < SIGNAL_DEDUPE_WINDOW_SECONDS}
        dedupe_key = (symbol, signal_type)
        if recent_signals.get(dedupe_key) is not None:
            return reject("DUPLICATE", symbol,
                          f"duplicate entry within {SIGNAL_DEDUPE_WINDOW_SECONDS}s",
                          strategy=signal_type)

        try:
            in_use = open_exposure_count()
            if in_use >= MAX_CONCURRENT_POSITIONS:
                return reject("CONCURRENCY_CAP", symbol,
                              f"{in_use}/{MAX_CONCURRENT_POSITIONS} slots in use",
                              strategy=signal_type)
        except Exception as e:
            return reject("CONCURRENCY_CHECK_FAILED", symbol,
                          f"could not verify concurrency cap, rejecting for safety: {e}",
                          http_status=500, log_level="error", strategy=signal_type)

        try:
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
            equity = float(account.equity)
        except Exception as e:
            return reject("BUYING_POWER_CHECK_FAILED", symbol,
                          f"could not check buying power, rejecting for safety: {e}",
                          http_status=500, log_level="error", strategy=signal_type)

        # Sizing. With a cap of 1 this is identical to the previous
        # behaviour: the whole equity fraction of available buying power.
        #
        # With a cap above 1 the allocation is divided by the cap, so the
        # first position cannot consume the capital the second would need.
        # It is deliberately NOT "whatever is left" -- that would give the
        # strategy that happens to fire first a systematically larger
        # position, which would corrupt any comparison between them.
        allocation = buying_power * EQUITY_FRACTION
        if MAX_CONCURRENT_POSITIONS > 1:
            allocation = min(allocation, (equity * EQUITY_FRACTION) / MAX_CONCURRENT_POSITIONS)

        qty = int(allocation // entry_limit)
        if qty < 1:
            return reject("INSUFFICIENT_BP", symbol,
                          f"allocation ${allocation:.2f} insufficient at entry={entry_limit} "
                          f"(bp=${buying_power:.2f}, cap={MAX_CONCURRENT_POSITIONS})",
                          strategy=signal_type)

        try:
            # BRACKET: entry + take_profit + native stop, submitted together.
            # Alpaca links TP and SL as OCO siblings natively -- whichever
            # fires, the other is auto-cancelled on their side.
            order = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=round_tick(entry_limit),
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=round_tick(take_profit)),
                stop_loss=StopLossRequest(stop_price=round_tick(stop_loss)),
            )
            submitted = trading_client.submit_order(order)
            recent_signals[dedupe_key] = now
            pending_signal_types[symbol] = signal_type

            with strategy_stats_lock:
                strategy_stats[signal_type]["entries"] += 1

            # Capture the stop leg's id now, while the submit response still
            # contains it. Waiting until a stop_update arrives is too late --
            # by then the list endpoint has stopped returning it.
            captured = remember_stop_leg(symbol, submitted)
            if captured is None:
                logger.warning(
                    f"[LEG] {symbol} submit response contained no STOP leg — "
                    f"stop_update will have to fall back to scanning the order list"
                )

            latency_ms = (time.time() - start_time) * 1000
            note = "" if abs(buying_power - equity) < 1.0 else f" [WARN: BP/equity {buying_power / equity:.2f}x]"
            send_alert(
                f"[ENTRY] {symbol} [{signal_type}] qty={qty} limit={entry_limit} TP={take_profit} "
                f"SL={stop_loss} (stop dist {stop_distance_pct:.2f}% of cap {cap}%) | "
                f"equity=${equity:.2f} bp=${buying_power:.2f}{note} | Order ID: {submitted.id} | "
                f"Status: {submitted.status} | Latency: {latency_ms:.1f}ms"
            )
        except Exception as e:
            send_alert(f"[CRIT] Failed to submit entry order for {symbol}: {e}")
            return "Order submission failed", 500

    return "Success", 200


# ============================================================
# STOP UPDATE
# ============================================================
def handle_stop_update(data, symbol):
    """Replaces the native stop leg's price. Pine has already decided the
    level from TradingView data; this only moves the order.

    NO POSITION means Pine believes it is in a trade that never filled --
    Pine cannot see the Alpaca account, so this WILL happen and must be a
    quiet no-op rather than an error."""
    strategy = pending_signal_types.get(symbol)

    try:
        stop_price = round_tick(float(data["stop_price"]))
    except (KeyError, ValueError, TypeError) as e:
        return reject("MALFORMED", symbol, f"malformed stop_update payload — {e}",
                      http_status=400, strategy=strategy)

    try:
        position = get_position(symbol)
    except Exception as e:
        return reject("POSITION_CHECK_FAILED", symbol, f"could not verify position: {e}",
                      http_status=500, log_level="error", strategy=strategy)

    if position is None:
        return reject("NO_POSITION", symbol,
                      f"stop_update to ${stop_price} ignored — no open position "
                      f"(Pine believes it is in a trade that did not fill)",
                      log_level="debug", strategy=strategy)

    try:
        stop_orders = get_stop_orders(symbol)
    except Exception as e:
        return reject("ORDER_LOOKUP_FAILED", symbol, f"could not list stop orders: {e}",
                      http_status=500, log_level="error", strategy=strategy)

    if not stop_orders:
        return reject("NO_STOP_ORDER", symbol,
                      f"position open but NO stop order visible — cannot ratchet to ${stop_price}",
                      log_level="error", strategy=strategy)

    moved = 0
    for o in stop_orders:
        current_stop = float(o.stop_price)
        # Ratchet up only. Pine already enforces this, but a replayed or
        # out-of-order webhook must never loosen a stop.
        if stop_price <= current_stop:
            continue
        try:
            replaced = retry_replace_order(o.id, ReplaceOrderRequest(stop_price=stop_price))
            # A replace supersedes the order and issues a NEW id. Without
            # this the map would still point at the replaced order and every
            # later ratchet would fall back to the broken list scan.
            new_id = getattr(replaced, "id", None)
            if new_id is not None:
                with stop_leg_lock:
                    stop_leg_ids[symbol] = new_id
            # Log all three values. Alpaca can round or adjust what it
            # accepts, and "requested 18.42, accepted 18.42" vs
            # "requested 18.42, accepted 18.11" is the difference between a
            # working trail and one that silently is not moving.
            accepted = getattr(replaced, "stop_price", None)
            accepted_str = f"${float(accepted):.4f}" if accepted is not None else "unconfirmed"
            logger.info(
                f"[TRAIL] {symbol} [{strategy or '-'}] requested=${stop_price:.4f} "
                f"current=${current_stop:.4f} accepted={accepted_str}"
            )
            moved += 1
        except Exception as e:
            send_alert(f"[CRIT] Failed to move stop for {symbol} to ${stop_price}: {e}")
            return "Stop replace failed", 500

    if moved == 0:
        return reject("STOP_NOT_HIGHER", symbol,
                      f"requested ${stop_price} is not above the current stop — no change",
                      log_level="debug", strategy=strategy)

    return "Success", 200


# ============================================================
# EXIT
# ============================================================
def handle_exit(data, symbol):
    """Closes the position at market. Pine fires this on a confirmed close
    below the exit level, or at session end.

    Legs are cancelled BEFORE the close. Closing while a bracket sibling
    still rests is what produced the AMIX stray order."""
    reason = str(data.get("reason", "unspecified"))
    strategy = pending_signal_types.get(symbol)

    try:
        position = get_position(symbol)
    except Exception as e:
        return reject("POSITION_CHECK_FAILED", symbol, f"could not verify position: {e}",
                      http_status=500, log_level="error", strategy=strategy)

    if position is None:
        # No position, but a pending entry may still be resting. Pine has
        # decided the setup is dead, so that entry should not fill either.
        try:
            pending = [o for o in flatten_orders(get_open_orders(symbol)) if o.side == OrderSide.BUY]
        except Exception:
            pending = []
        if pending:
            for o in pending:
                try:
                    retry_cancel_order(o.id)
                except Exception as e:
                    logger.warning(f"Could not cancel pending entry {o.id} for {symbol}: {e}")
            pending_signal_types.pop(symbol, None)
            send_alert(f"[EXIT] {symbol} — no position; cancelled {len(pending)} pending entry order(s) ({reason})")
            return "Success", 200
        return reject("NO_POSITION", symbol, f"exit ({reason}) ignored — nothing open",
                      log_level="debug", strategy=strategy)

    qty = position.qty
    cancelled = cancel_resting_legs(symbol)

    # Cancellation is asynchronous. Confirm the legs are actually gone before
    # submitting the market close, rather than assuming the accepted cancel
    # requests took effect immediately.
    clear, remaining = verify_legs_cancelled(symbol)
    if not clear and remaining:
        logger.warning(
            f"[EXIT] {symbol} — {len(remaining)} sell leg(s) still visible after cancel "
            f"({[str(o.id) for o in remaining]}); closing anyway rather than leaving the position open"
        )

    if close_position_with_retry(symbol, reason):
        send_alert(
            f"[EXIT] {symbol} [{strategy or '-'}] — closing {qty} shares at market ({reason}); "
            f"cancelled {cancelled} resting leg(s), legs_clear={clear}"
        )
        return "Success", 200

    return "Close failed", 500


# ============================================================
# WEBHOOK
# ============================================================
@app.route("/", methods=["POST"])
def webhook():
    start_time = time.time()

    if not manager_status["is_alive"] or (time.time() - manager_status["last_heartbeat"] > 60):
        send_alert("[CRIT] Manager thread offline — signal rejected.")
        return "System Offline", 503

    data = request.get_json(force=True, silent=True)
    if not data:
        # Usually TradingView's periodic connectivity test pings, not real
        # signals. Tallied separately so they don't pollute the summary.
        return reject("NO_JSON", "-", "no/invalid JSON body received", http_status=400)

    if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
        return reject("BAD_SECRET", data.get("symbol", "-"), "invalid webhook secret", http_status=401)

    symbol = str(data.get("symbol", "UNKNOWN")).upper()
    msg_type = str(data.get("type", "entry")).lower()
    signal_type = str(data.get("signal", DEFAULT_STRATEGY_TAG)).upper()

    if msg_type == "entry":
        return handle_entry(data, symbol, signal_type, start_time)
    if msg_type == "stop_update":
        return handle_stop_update(data, symbol)
    if msg_type == "exit":
        return handle_exit(data, symbol)

    return reject("UNKNOWN_TYPE", symbol, f"unrecognised message type '{msg_type}'",
                  http_status=400, strategy=signal_type)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))
























































































































