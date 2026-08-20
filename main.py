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

# Defense-in-depth: reject any signal whose implied stop distance exceeds
# this, regardless of what Pine computed — full-size sizing means this is
# the only backstop against an outlier candle producing an outsized loss.
# stop_loss is a real resting order again (BRACKET stop-loss leg), so this
# directly protects that order's risk, not just a reference value.
MAX_STOP_DISTANCE_PCT = float(os.getenv("MAX_STOP_DISTANCE_PCT", "15.0"))

# Guards against a signal firing on a condition that was true when the
# signal bar closed but no longer reflects reality by the time the order
# is about to submit (late/revised tape prints on thin small caps).
# Historically this rejected trades whose live price had already crossed
# stop_loss, but that check is now informational-only (see the webhook's
# live-quote block below) -- kept as a comment/marker for context, no
# active config needed for it anymore.

# Used purely for informational logging now (see webhook). Alpaca's own
# quote on the free IEX-only plan proved unreliable on thin small caps
# (confirmed live with TNON: entry $11.51, quote came back $4.55; and
# RDAC: entry $14.33, quote came back $7.84 -- both far outside anything
# a real tick-to-tick move could produce). Entry/stop/take-profit all come
# from TradingView's real-time feed already, and submitting a limit order
# to Alpaca doesn't require accurate quote data on our side -- the order
# routes and fills against the real market regardless of data plan. So a
# large drift here is logged as likely-bad-Alpaca-data, never blocks.
MAX_PLAUSIBLE_PRICE_DRIFT_PCT = float(os.getenv("MAX_PLAUSIBLE_PRICE_DRIFT_PCT", "40.0"))

# Flat trailing-stop percentage. The stop only ever ratchets UP (native STOP
# order price is replaced, never loosened) toward highest-price-since-entry
# minus this percent. Take-profit (manual target, set in Pine) is the
# primary exit target; this is the fallback if that target is never reached.
# Active from entry -- the native stop leg is submitted as part of the
# BRACKET order itself, not created later at a gain threshold.
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
    last_eod_failure_alert = 0.0
    # Tracks currently-open positions so we can detect the moment one closes
    # (TP fill, trailing-stop fill, or EOD flatten) and log a single clean
    # summary line instead of noisy per-minute updates.
    open_positions_tracked = {}  # symbol -> {"qty", "entry_price"}
    # Tracks order IDs a cancel has already been requested for, so the
    # stale-entry cleanup below doesn't repeatedly re-issue cancel requests
    # every 2s loop cycle while Alpaca is still processing the first one
    # (which otherwise spams "[CRIT] ... order pending cancel" every cycle
    # until it actually clears). Pruned each cycle against currently-open
    # order IDs so it can't grow unbounded over the session.
    cancel_requested_order_ids = set()

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
                    # Daily reject-reason summary, right alongside the
                    # flatten -- one glance tells you how many signals
                    # fired vs. how many were rejected and why, before the
                    # tally resets for tomorrow. Only logged on a SUCCESSFUL
                    # flatten so it can't fire repeatedly during retries
                    # below and wipe the tally mid-day.
                    log_reject_summary()
                    # Only mark today's flatten as done on SUCCESS. If this
                    # were set unconditionally (as it was before), a single
                    # transient failure here would silently stop the bot
                    # from ever attempting to flatten again for the rest of
                    # the day -- a position could sit open overnight with
                    # no further protection beyond the trailing stop.
                    eod_flatten_triggered_date = current_date_str
                except Exception as e:
                    now_ts = time.time()
                    # Retry every loop cycle (~2s) until it succeeds -- but
                    # throttle the alert itself to once per 60s so a
                    # persistent outage doesn't spam the log into
                    # unreadability while still retrying the actual close
                    # attempt at full frequency underneath.
                    if now_ts - last_eod_failure_alert >= 60:
                        send_alert(f"[CRIT] EOD Flatten failed: {e} — retrying every cycle until it succeeds.")
                        last_eod_failure_alert = now_ts

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
                    # 'after' is the hard fix: only orders submitted after
                    # THIS position was first observed open can possibly be
                    # its exit. A small safety buffer (5 min back) covers
                    # clock/detection lag without reopening the door to an
                    # older session's orders, which are realistically hours
                    # or days apart, not minutes.
                    lookback_start = info["opened_at"] - timedelta(minutes=5)
                    recent_orders = trading_client.get_orders(
                        filter=GetOrdersRequest(
                            status=QueryOrderStatus.CLOSED,
                            symbols=[symbol],
                            limit=10,
                            after=lookback_start,
                        )
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

            # Track newly-opened positions with their true entry fill price
            # AND the moment we first observed them open. That timestamp is
            # what lets close-detection below only ever consider orders
            # from THIS specific trade -- without it, a stale closed order
            # from an earlier session on the same symbol can get mistaken
            # for this trade's real exit (confirmed live: EHGO logged a
            # bogus "$3.19 exit, qty 302, +39.3% WIN" that matched no real
            # order in Alpaca's own history -- the real exit was $2.24,
            # qty 356, a small loss -- because the lookup had no time
            # bound and grabbed an unrelated older filled order).
            for p in positions:
                if p.symbol not in open_positions_tracked:
                    open_positions_tracked[p.symbol] = {
                        "qty": float(p.qty),
                        "entry_price": float(p.avg_entry_price),
                        "opened_at": datetime.now(timezone.utc),
                    }

            all_open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=QueryOrderStatus.OPEN))

            open_orders_map = {}
            for o in all_open_orders:
                if o.type == OrderType.STOP:
                    open_orders_map.setdefault(o.symbol, []).append(o)

            # Prune the cancel-requested tracking set to only IDs still
            # actually open -- once an order leaves the open list (filled
            # or fully cancelled), there's no need to keep remembering it.
            open_order_ids_now = {o.id for o in all_open_orders}
            cancel_requested_order_ids &= open_order_ids_now

            # Stale entry order cleanup: an unfilled BUY entry that's been
            # sitting too long blocks the concurrency cap from freeing up.
            # Cancel is only ever REQUESTED once per order -- Alpaca
            # processes cancellation asynchronously, so an order can still
            # show up as "open" for a cycle or two after the request goes
            # out; re-issuing cancel on every cycle just bounces off
            # Alpaca with "order pending cancel" and spams false [CRIT]
            # alerts instead of reflecting anything actually wrong.
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

                    with hwm_lock:
                        if symbol not in global_hwm or current > global_hwm[symbol]:
                            global_hwm[symbol] = current
                            maybe_save_hwm(global_hwm, symbol=symbol, current_price=current)
                        hwm_val = global_hwm.get(symbol, current)

                    # Stop management: three floors combine, whichever is
                    # highest wins (the stop only ever ratchets up):
                    # 1) 15% trail: (highest price since entry) x 0.85 --
                    #    the fallback exit if TP is never reached.
                    # 2) Breakeven (+2%): stop >= entry, so a full reversal
                    #    from a modest gain never turns into a real loss.
                    # 3) Tier-2 lock (+8% -> lock +4%): once the trade has
                    #    proven real strength, lock in an actual profit
                    #    cushion instead of leaving the floor pinned at pure
                    #    breakeven all the way out to where the trail alone
                    #    would reach entry (~+17.6%) -- that gap is exactly
                    #    where a normal pullback-then-continuation wipes out
                    #    the whole gain for nothing.
                    # The native STOP order already exists from entry (it's
                    # the stop_loss leg of the BRACKET order submitted in
                    # the webhook) -- this loop only ever replaces its
                    # price, active continuously from the moment the
                    # position opens, not gated behind a gain threshold.
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

                        for order in open_orders_map[symbol]:
                            current_stop = float(order.stop_price)
                            if desired_stop > current_stop:
                                # Alpaca requires whole-cent stop prices for
                                # anything trading >= $1 -- sub-penny is
                                # only valid under $1. This unconditional
                                # 4-decimal round silently broke the
                                # trailing stop on RDAC (~$18): every
                                # replace call was rejected with "sub-penny
                                # increment does not fulfill minimum
                                # pricing criteria", so the stop stayed
                                # frozen at its original price instead of
                                # ratcheting up while the position ran.
                                tick = 0.01 if desired_stop >= 1.0 else 0.0001
                                rounded_stop = round(round(desired_stop / tick) * tick, 4)
                                try:
                                    retry_replace_order(
                                        order.id, ReplaceOrderRequest(stop_price=rounded_stop)
                                    )
                                    tag = be_tag or "[TRAIL]"
























































































































