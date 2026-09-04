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
feed. All levels come from TradingView and arrive as instructions.

TWO ENTRY MODES
---------------
1. BRACKET (regular hours). entry + take_profit + native stop submitted
   together as one Alpaca bracket. The stop rests at the exchange, and
   stop_update / exit messages manage it. This is the automated path.

2. MANUAL (premarket). A plain LIMIT order with extended_hours=True and
   NO protective legs, because Alpaca rejects stop and stop-limit orders
   outside regular hours — they are refused outright or sit inactive
   until 09:30 ET. The operator sets the stop and target by hand after
   the fill.

   A manual entry is requested by sending "managed":"manual" in the
   payload, or by sending take_profit and stop_loss as 0.

   THE POSITION IS UNPROTECTED UNTIL THE OPERATOR ACTS. That is a
   deliberate choice, not an oversight, so the naked-position watchdog
   warns once at entry rather than screaming every cycle — but the
   exposure is real and belongs to whoever is awake.

Inbound message types:

  entry        -> bracket order, or plain extended-hours limit if manual
  stop_update  -> replace the native stop leg's price (ratchet up only)
  exit         -> cancel resting legs, then close the position at market
"""

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
    Melbourne time (what the operator sees when reviewing)."""

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

ENTRY_ORDER_TIMEOUT_SECONDS = int(os.getenv("ENTRY_ORDER_TIMEOUT_SECONDS", "300"))

# Two windows, both ET.
#
# BRACKET window: regular hours, where a protective stop can rest at the
# exchange. This is the automated, walk-away path.
TRADING_WINDOW_START = os.getenv("TRADING_WINDOW_START", "09:35")
TRADING_WINDOW_END = os.getenv("TRADING_WINDOW_END", "11:35")

# MANUAL window: premarket. Entries here carry NO protective legs, so the
# window is deliberately narrow and the operator must be at the screen.
PREMARKET_WINDOW_START = os.getenv("PREMARKET_WINDOW_START", "04:00")
PREMARKET_WINDOW_END = os.getenv("PREMARKET_WINDOW_END", "09:29")

# Alpaca stops accepting extended-hours orders at 09:30. Cutting off two
# minutes early avoids a race where the order is accepted premarket but
# routes into the opening auction.
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "1"))

EQUITY_FRACTION = float(os.getenv("EQUITY_FRACTION", "0.98"))

# Manual entries carry no stop, so this cap cannot protect them. It only
# applies to bracket entries, where a stop price is actually supplied.
MAX_STOP_DISTANCE_PCT = float(os.getenv("MAX_STOP_DISTANCE_PCT", "15.0"))

# Manual premarket entries are unprotected by design. This caps how much
# of the account can sit in one, independently of EQUITY_FRACTION, because
# "full size with no stop" is a different risk from "full size with a stop".
MANUAL_EQUITY_FRACTION = float(os.getenv("MANUAL_EQUITY_FRACTION", "0.50"))

ORDERS_DEBUG_INTERVAL_SECONDS = int(os.getenv("ORDERS_DEBUG_INTERVAL_SECONDS", "30"))

DEFAULT_SIGNAL_TAG = os.getenv("DEFAULT_SIGNAL_TAG", "RF_FLIP")

order_lock = threading.Lock()

recent_signals = {}
SIGNAL_DEDUPE_WINDOW_SECONDS = 15

pending_signal_types = {}

# Symbols entered via the manual path. Their positions have no stop leg BY
# DESIGN, so the naked-position watchdog must not treat them as an
# emergency every cycle.
manual_managed_lock = threading.Lock()
manual_managed_symbols = set()

reject_tally_lock = threading.Lock()
reject_reason_counts = {}

signal_stats_lock = threading.Lock()
signal_stats = defaultdict(lambda: {"entries": 0, "wins": 0, "losses": 0, "pl": 0.0})

force_close_lock = threading.Lock()
force_close_symbols = set()

# symbol -> order id of the bracket's STOP leg, captured from the submit
# response at entry time.
#
# WHY THIS EXISTS: the open-orders LIST endpoint does not reliably return
# both OCO siblings once the parent entry has filled. Confirmed live across
# four trades -- while the entry was unfilled both legs showed as HELD and
# nested, then the moment the entry left the open list only the take-profit
# LIMIT came back and the STOP disappeared. Looking the leg up by its own
# id sidesteps the list endpoint entirely.
stop_leg_lock = threading.Lock()
stop_leg_ids = {}


def mark_manual(symbol):
    with manual_managed_lock:
        manual_managed_symbols.add(symbol)


def unmark_manual(symbol):
    with manual_managed_lock:
        manual_managed_symbols.discard(symbol)


def is_manual(symbol):
    with manual_managed_lock:
        return symbol in manual_managed_symbols


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


def reject(tag, symbol, message, http_status=200, log_level="info", sig=None):
    """Central reject helper. Every rejection MUST go through this so the
    reason is tagged distinctly in the log and tallied for the daily
    summary."""
    key = f"{tag}|{sig.upper()}" if sig else tag
    with reject_tally_lock:
        reject_reason_counts[key] = reject_reason_counts.get(key, 0) + 1
    prefix = f"[REJECT:{tag}]" if not sig else f"[REJECT:{tag}][{sig.upper()}]"
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


def log_signal_summary():
    with signal_stats_lock:
        if not signal_stats:
            logger.info("[SUMMARY] No entries taken today.")
        else:
            for tag, s in sorted(signal_stats.items()):
                closed = s["wins"] + s["losses"]
                wr = (s["wins"] / closed * 100) if closed else 0.0
                logger.info(
                    f"[SUMMARY] {tag}: entries={s['entries']} closed={closed} "
                    f"W/L={s['wins']}/{s['losses']} ({wr:.0f}%) P/L=${s['pl']:.2f}"
                )
        signal_stats.clear()


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


def _in_window(start_str, end_str):
    now_ny = datetime.now(NY_TZ).time()
    start = datetime.strptime(start_str, "%H:%M").time()
    end = datetime.strptime(end_str, "%H:%M").time()
    return start <= now_ny <= end


def within_trading_window():
    return _in_window(TRADING_WINDOW_START, TRADING_WINDOW_END)


def within_premarket_window():
    return _in_window(PREMARKET_WINDOW_START, PREMARKET_WINDOW_END)


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
    take-profit LIMIT leg -- the stop leg was absent, yet demonstrably alive.
    Alpaca returns the bracket's OCO siblings as LEGS of one another rather
    than as separate top-level orders, so a flat query silently loses one."""
    if symbol:
        f = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol], nested=nested)
    else:
        f = GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=nested)
    return trading_client.get_orders(filter=f)


def flatten_orders(orders):
    """Walks an order list and yields every order AND every nested leg."""
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
    """The live STOP order(s) for a symbol. Tries the remembered leg id
    FIRST, because the list endpoint drops the stop sibling once the entry
    fills."""
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
    """How many slots are consumed: open positions plus resting unfilled
    entries, counted as distinct symbols."""
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
    later against a position that no longer exists."""
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
    Bounded on purpose: waiting indefinitely would be worse than a stray
    order, because the position stays open the whole time."""
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
    """Closes a position, retrying on failure. A close that fails AFTER the
    legs were cancelled leaves an unprotected position."""
    for attempt in range(1, attempts + 1):
        try:
            trading_client.close_position(symbol)
            return True
        except Exception as e:
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
# ============================================================
def position_manager_loop():
    last_equity_log = 0.0
    eod_flatten_triggered_date = None
    last_eod_failure_alert = 0.0
    last_orders_debug = 0.0

    open_positions_tracked = {}
    cancel_requested_order_ids = set()
    naked_streak = {}
    manual_reminder_sent = set()

    while True:
        try:
            manager_status["last_heartbeat"] = time.time()
            manager_status["is_alive"] = True
            now = time.time()
            now_ny_dt = datetime.now(NY_TZ)

            # EOD flatten at 15:55 ET, keyed off the NEW YORK date: the
            # operator is in Melbourne where one US session spans two local
            # dates, so a local-date key would roll over mid-session.
            #
            # This also catches a manual premarket position the operator
            # forgot about, which is the main automated backstop those
            # entries have.
            current_date_str = now_ny_dt.strftime("%Y-%m-%d")
            eod_target_time = datetime.strptime("15:55", "%H:%M").time()

            if now_ny_dt.time() >= eod_target_time and eod_flatten_triggered_date != current_date_str:
                send_alert("[EOD] Reached 15:55 ET. Flattening all positions and cancelling open orders.")
                try:
                    trading_client.close_all_positions(cancel_orders=True)
                    send_alert("[EOD] Successfully closed all positions.")
                    time.sleep(3)
                    log_reject_summary()
                    log_signal_summary()
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

            # Close detection.
            for symbol in set(open_positions_tracked.keys()) - current_symbols:
                info = open_positions_tracked.pop(symbol)
                sig = info.get("signal", DEFAULT_SIGNAL_TAG)
                try:
                    lookback_start = info["opened_at"] - timedelta(minutes=5)

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
                        with signal_stats_lock:
                            s = signal_stats[sig]
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
                manual_reminder_sent.discard(symbol)
                unmark_manual(symbol)
                forget_stop_leg(symbol)

            for p in positions:
                if p.symbol not in open_positions_tracked:
                    open_positions_tracked[p.symbol] = {
                        "qty": float(p.qty),
                        "entry_price": float(p.avg_entry_price),
                        "opened_at": datetime.now(timezone.utc),
                        "signal": pending_signal_types.get(p.symbol, DEFAULT_SIGNAL_TAG),
                    }
                else:
                    open_positions_tracked[p.symbol]["qty"] = float(p.qty)
                    open_positions_tracked[p.symbol]["entry_price"] = float(p.avg_entry_price)

            all_open_orders = flatten_orders(get_open_orders())

            # Naked-position watchdog.
            #
            # A BRACKET position with no live stop is an emergency and keeps
            # alerting. A MANUAL position has no stop by design, so it gets
            # ONE reminder instead — the operator already knows, and an alert
            # that fires every cycle is an alert nobody reads.
            for p in positions:
                if is_manual(p.symbol):
                    if p.symbol not in manual_reminder_sent:
                        manual_reminder_sent.add(p.symbol)
                        send_alert(
                            f"[MANUAL] {p.symbol} filled premarket with NO protective stop (qty {p.qty}). "
                            f"Set the stop and target by hand. EOD flatten at 15:55 ET is the only "
                            f"automated backstop."
                        )
                    continue
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

            if positions and (now - last_orders_debug) >= ORDERS_DEBUG_INTERVAL_SECONDS:
                stop_orders = [o for o in all_open_orders if o.type == OrderType.STOP]
                logger.info(
                    f"[ORDERS] visible={[(o.symbol, str(o.type).split('.')[-1], str(o.status).split('.')[-1], str(o.stop_price)) for o in all_open_orders]} "
                    f"| stop_legs={[(o.symbol, str(o.stop_price)) for o in stop_orders]} "
                    f"| positions={[p.symbol for p in positions]} "
                    f"| manual={sorted(manual_managed_symbols)} "
                    f"| remembered_legs={dict(stop_leg_ids)}"
                )
                last_orders_debug = now

            cancel_requested_order_ids &= {o.id for o in all_open_orders}

            # Stale entry cleanup.
            for o in all_open_orders:
                if o.side == OrderSide.BUY:
                    age = (datetime.now(timezone.utc) - o.created_at).total_seconds()
                    if age >= ENTRY_ORDER_TIMEOUT_SECONDS and o.id not in cancel_requested_order_ids:
                        try:
                            retry_cancel_order(o.id)
                            cancel_requested_order_ids.add(o.id)
                            # An entry that never fills never closes, so
                            # without this the tag and the manual flag leak
                            # and mislabel the next trade on that symbol.
                            pending_signal_types.pop(o.symbol, None)
                            unmark_manual(o.symbol)
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
    logger.info(
        f"[CONFIG] bracket_window={TRADING_WINDOW_START}-{TRADING_WINDOW_END} ET | "
        f"premarket_manual_window={PREMARKET_WINDOW_START}-{PREMARKET_WINDOW_END} ET | "
        f"equity_fraction={EQUITY_FRACTION} manual_fraction={MANUAL_EQUITY_FRACTION} | "
        f"max_concurrent={MAX_CONCURRENT_POSITIONS} | max_stop_dist={MAX_STOP_DISTANCE_PCT}% | "
        f"entry_timeout={ENTRY_ORDER_TIMEOUT_SECONDS}s | default_signal_tag={DEFAULT_SIGNAL_TAG}"
    )
    logger.info(
        "[CONFIG] Manual premarket entries carry NO protective stop — Alpaca rejects stop "
        "orders outside regular hours. The operator sets stop and target by hand; EOD "
        "flatten at 15:55 ET is the only automated backstop."
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
        "manual_positions": sorted(manual_managed_symbols),
        "uptime_hours": round((time.time() - bot_start_time) / 3600.0, 2),
        "now_et": datetime.now(NY_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "now_melbourne": datetime.now(MEL_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }
    return (payload, 200) if is_healthy else (payload, 500)


@app.route("/reject-summary", methods=["GET"])
def reject_summary():
    with reject_tally_lock:
        return dict(reject_reason_counts), 200


@app.route("/signal-summary", methods=["GET"])
def signal_summary():
    with signal_stats_lock:
        return {k: dict(v) for k, v in signal_stats.items()}, 200


# ============================================================
# ENTRY
# ============================================================
def handle_entry(data, symbol, signal_type, start_time):
    try:
        entry_limit = float(data["entry_limit"])
    except (KeyError, ValueError, TypeError) as e:
        return reject("MALFORMED", symbol, f"malformed entry payload — {e}",
                      http_status=400, sig=signal_type)

    # take_profit / stop_loss are optional on a manual entry.
    try:
        stop_loss = float(data.get("stop_loss", 0) or 0)
        take_profit = float(data.get("take_profit", 0) or 0)
    except (ValueError, TypeError) as e:
        return reject("MALFORMED", symbol, f"malformed tp/sl values — {e}",
                      http_status=400, sig=signal_type)

    # Manual mode: explicitly requested, or implied by both legs being zero.
    managed = str(data.get("managed", "")).lower()
    manual_mode = managed == "manual" or (stop_loss == 0 and take_profit == 0)

    if manual_mode:
        if not within_premarket_window():
            now_ny_str = datetime.now(NY_TZ).strftime("%H:%M:%S")
            return reject(
                "PREMARKET_WINDOW", symbol,
                f"manual entry arrived at {now_ny_str} ET, outside "
                f"{PREMARKET_WINDOW_START}-{PREMARKET_WINDOW_END} ET (entry={entry_limit})",
                sig=signal_type,
            )
        if entry_limit <= 0:
            return reject("ENTRY_INVALID", symbol, f"entry_limit must be positive (got {entry_limit})",
                          sig=signal_type)
    else:
        if not within_trading_window():
            now_ny_str = datetime.now(NY_TZ).strftime("%H:%M:%S")
            return reject(
                "WINDOW", symbol,
                f"arrived at {now_ny_str} ET, outside {TRADING_WINDOW_START}-{TRADING_WINDOW_END} ET "
                f"(entry={entry_limit}, sl={stop_loss}, tp={take_profit})",
                sig=signal_type,
            )
        if not stop_loss > 0:
            return reject("SL_INVALID", symbol, f"stop_loss must be positive (got {stop_loss})", sig=signal_type)
        if not stop_loss < entry_limit:
            return reject("SL_GE_ENTRY", symbol, f"stop_loss ({stop_loss}) must be below entry ({entry_limit})",
                          sig=signal_type)
        if take_profit == 0.0:
            return reject("TP_NOT_SET", symbol, "take_profit is 0.0 — target not set on this ticker's chart",
                          sig=signal_type)
        if not entry_limit < take_profit:
            return reject("TP_LE_ENTRY", symbol, f"entry ({entry_limit}) must be below take_profit ({take_profit})",
                          sig=signal_type)

        stop_distance_pct = ((entry_limit - stop_loss) / entry_limit) * 100
        if stop_distance_pct > MAX_STOP_DISTANCE_PCT:
            return reject(
                "STOP_TOO_WIDE", symbol,
                f"stop distance {stop_distance_pct:.2f}% exceeds MAX_STOP_DISTANCE_PCT={MAX_STOP_DISTANCE_PCT}%",
                sig=signal_type,
            )

    with order_lock:
        now = time.time()
        global recent_signals
        recent_signals = {k: t for k, t in recent_signals.items() if now - t < SIGNAL_DEDUPE_WINDOW_SECONDS}
        dedupe_key = (symbol, signal_type)
        if recent_signals.get(dedupe_key) is not None:
            return reject("DUPLICATE", symbol, f"duplicate entry within {SIGNAL_DEDUPE_WINDOW_SECONDS}s",
                          sig=signal_type)

        try:
            in_use = open_exposure_count()
            if in_use >= MAX_CONCURRENT_POSITIONS:
                return reject("CONCURRENCY_CAP", symbol,
                              f"{in_use}/{MAX_CONCURRENT_POSITIONS} slots in use", sig=signal_type)
        except Exception as e:
            return reject("CONCURRENCY_CHECK_FAILED", symbol,
                          f"could not verify concurrency cap, rejecting for safety: {e}",
                          http_status=500, log_level="error", sig=signal_type)

        try:
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
            equity = float(account.equity)
        except Exception as e:
            return reject("BUYING_POWER_CHECK_FAILED", symbol,
                          f"could not check buying power, rejecting for safety: {e}",
                          http_status=500, log_level="error", sig=signal_type)

        # A manual entry has no stop, so it is sized off its own fraction.
        fraction = MANUAL_EQUITY_FRACTION if manual_mode else EQUITY_FRACTION
        allocation = buying_power * fraction
        if MAX_CONCURRENT_POSITIONS > 1:
            allocation = min(allocation, (equity * fraction) / MAX_CONCURRENT_POSITIONS)

        qty = int(allocation // entry_limit)
        if qty < 1:
            return reject("INSUFFICIENT_BP", symbol,
                          f"allocation ${allocation:.2f} insufficient at entry={entry_limit} "
                          f"(bp=${buying_power:.2f}, fraction={fraction})",
                          sig=signal_type)

        try:
            if manual_mode:
                # PLAIN LIMIT, extended hours. Alpaca requires TIF=DAY for
                # extended-hours orders and refuses bracket/OCO entirely
                # outside regular hours, which is why there are no legs.
                order = LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                    limit_price=round_tick(entry_limit),
                    extended_hours=True,
                )
            else:
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

            with signal_stats_lock:
                signal_stats[signal_type]["entries"] += 1

            if manual_mode:
                mark_manual(symbol)
            else:
                unmark_manual(symbol)
                captured = remember_stop_leg(symbol, submitted)
                if captured is None:
                    logger.warning(
                        f"[LEG] {symbol} submit response contained no STOP leg — "
                        f"stop_update will have to fall back to scanning the order list"
                    )

            latency_ms = (time.time() - start_time) * 1000
            note = "" if abs(buying_power - equity) < 1.0 else f" [WARN: BP/equity {buying_power / equity:.2f}x]"
            if manual_mode:
                send_alert(
                    f"[ENTRY-MANUAL] {symbol} [{signal_type}] qty={qty} limit={entry_limit} "
                    f"extended_hours=True NO TP/SL — SET THEM BY HAND ONCE FILLED | "
                    f"equity=${equity:.2f} bp=${buying_power:.2f}{note} | Order ID: {submitted.id} | "
                    f"Status: {submitted.status} | Latency: {latency_ms:.1f}ms"
                )
            else:
                stop_distance_pct = ((entry_limit - stop_loss) / entry_limit) * 100
                send_alert(
                    f"[ENTRY] {symbol} [{signal_type}] qty={qty} limit={entry_limit} TP={take_profit} "
                    f"SL={stop_loss} (stop dist {stop_distance_pct:.2f}%) | equity=${equity:.2f} "
                    f"bp=${buying_power:.2f}{note} | Order ID: {submitted.id} | "
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
    level from TradingView data; this only moves the order."""
    sig = pending_signal_types.get(symbol)

    try:
        stop_price = round_tick(float(data["stop_price"]))
    except (KeyError, ValueError, TypeError) as e:
        return reject("MALFORMED", symbol, f"malformed stop_update payload — {e}",
                      http_status=400, sig=sig)

    if is_manual(symbol):
        return reject("MANUAL_POSITION", symbol,
                      f"stop_update to ${stop_price} ignored — {symbol} was entered manually "
                      f"and has no bot-managed stop leg",
                      log_level="debug", sig=sig)

    try:
        position = get_position(symbol)
    except Exception as e:
        return reject("POSITION_CHECK_FAILED", symbol, f"could not verify position: {e}",
                      http_status=500, log_level="error", sig=sig)

    if position is None:
        return reject("NO_POSITION", symbol,
                      f"stop_update to ${stop_price} ignored — no open position "
                      f"(Pine believes it is in a trade that did not fill)",
                      log_level="debug", sig=sig)

    try:
        stop_orders = get_stop_orders(symbol)
    except Exception as e:
        return reject("ORDER_LOOKUP_FAILED", symbol, f"could not list stop orders: {e}",
                      http_status=500, log_level="error", sig=sig)

    if not stop_orders:
        return reject("NO_STOP_ORDER", symbol,
                      f"position open but NO stop order visible — cannot ratchet to ${stop_price}",
                      log_level="error", sig=sig)

    moved = 0
    for o in stop_orders:
        current_stop = float(o.stop_price)
        # Ratchet up only. Pine already enforces this, but a replayed or
        # out-of-order webhook must never loosen a stop.
        if stop_price <= current_stop:
            continue
        try:
            replaced = retry_replace_order(o.id, ReplaceOrderRequest(stop_price=stop_price))
            new_id = getattr(replaced, "id", None)
            if new_id is not None:
                with stop_leg_lock:
                    stop_leg_ids[symbol] = new_id
            accepted = getattr(replaced, "stop_price", None)
            accepted_str = f"${float(accepted):.4f}" if accepted is not None else "unconfirmed"
            logger.info(
                f"[TRAIL] {symbol} [{sig or '-'}] requested=${stop_price:.4f} "
                f"current=${current_stop:.4f} accepted={accepted_str}"
            )
            moved += 1
        except Exception as e:
            send_alert(f"[CRIT] Failed to move stop for {symbol} to ${stop_price}: {e}")
            return "Stop replace failed", 500

    if moved == 0:
        # Logged at INFO, not debug: silence here is indistinguishable from
        # Pine never sending anything, which is exactly the ambiguity that
        # made the trail impossible to verify.
        return reject("STOP_NOT_HIGHER", symbol,
                      f"requested ${stop_price} is not above the current stop — no change",
                      sig=sig)

    return "Success", 200


# ============================================================
# EXIT
# ============================================================
def handle_exit(data, symbol):
    """Closes the position at market. Legs are cancelled BEFORE the close —
    closing while a bracket sibling still rests is what produced the AMIX
    stray order."""
    reason = str(data.get("reason", "unspecified"))
    sig = pending_signal_types.get(symbol)

    try:
        position = get_position(symbol)
    except Exception as e:
        return reject("POSITION_CHECK_FAILED", symbol, f"could not verify position: {e}",
                      http_status=500, log_level="error", sig=sig)

    if position is None:
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
            unmark_manual(symbol)
            send_alert(f"[EXIT] {symbol} — no position; cancelled {len(pending)} pending entry order(s) ({reason})")
            return "Success", 200
        return reject("NO_POSITION", symbol, f"exit ({reason}) ignored — nothing open",
                      log_level="debug", sig=sig)

    qty = position.qty
    cancelled = cancel_resting_legs(symbol)

    clear, remaining = verify_legs_cancelled(symbol)
    if not clear and remaining:
        logger.warning(
            f"[EXIT] {symbol} — {len(remaining)} sell leg(s) still visible after cancel "
            f"({[str(o.id) for o in remaining]}); closing anyway rather than leaving the position open"
        )

    if close_position_with_retry(symbol, reason):
        unmark_manual(symbol)
        send_alert(
            f"[EXIT] {symbol} [{sig or '-'}] — closing {qty} shares at market ({reason}); "
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
        return reject("NO_JSON", "-", "no/invalid JSON body received", http_status=400)

    if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
        return reject("BAD_SECRET", data.get("symbol", "-"), "invalid webhook secret", http_status=401)

    symbol = str(data.get("symbol", "UNKNOWN")).upper()
    msg_type = str(data.get("type", "entry")).lower()
    signal_type = str(data.get("signal", DEFAULT_SIGNAL_TAG)).upper()

    if msg_type == "entry":
        return handle_entry(data, symbol, signal_type, start_time)
    if msg_type == "stop_update":
        return handle_stop_update(data, symbol)
    if msg_type == "exit":
        return handle_exit(data, symbol)

    return reject("UNKNOWN_TYPE", symbol, f"unrecognised message type '{msg_type}'",
                  http_status=400, sig=signal_type)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))























































































































