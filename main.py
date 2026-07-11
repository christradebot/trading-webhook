import json
import logging
import os
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
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
# Logging — persistent file log + console, so anything weird
# that happens overnight is still readable weeks later.
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
logger = logging.getLogger("trading_bot")

app = Flask(__name__)

# SANITY CHECK: Prints status on startup
is_live = os.getenv("LIVE_TRADING", "False") == "True"
logger.info(f"--- BOT STARTED: LIVE_TRADING={is_live} ---")
trading_client = TradingClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"), paper=not is_live)

HWM_FILE = "hwm_data.json"
manager_status = {"last_heartbeat": time.time(), "is_alive": True}
symbol_error_counts = {}
symbol_alert_cooldown = {}  # tracks last alert count sent, to avoid spam
ERROR_ALERT_INTERVAL = 10   # once past threshold, re-alert every Nth failure

# Risk / gating config — all overridable via env vars without touching code.
MAX_POSITION_SIZE = int(os.getenv("MAX_POSITION_SIZE", "500"))
TRADING_WINDOW_START = os.getenv("TRADING_WINDOW_START", "09:45")  # ET, 24h format
TRADING_WINDOW_END = os.getenv("TRADING_WINDOW_END", "20:00")      # ET, 24h format
NY_TZ = ZoneInfo("America/New_York")

# Idempotency: a lock serializes the check-then-submit sequence so two
# near-simultaneous webhook deliveries can't both pass has_open_exposure()
# before either has submitted an order. The recent-signal cache is a second,
# belt-and-braces layer in case Alpaca's own position/order data lags by a
# moment right after submission.
order_lock = threading.Lock()
recent_signals = {}  # symbol -> timestamp of last accepted signal
SIGNAL_DEDUPE_WINDOW_SECONDS = 5

# HWM disk writes are throttled so a fast-moving position doesn't hammer disk
# on every tick. In-memory value is always current; only the write is delayed.
HWM_SAVE_INTERVAL_SECONDS = 20
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
    """Throttled HWM persistence. Use force=True for state changes that must
    survive a restart immediately (e.g. a position closing)."""
    global _last_hwm_save
    now = time.time()
    if force or (now - _last_hwm_save) >= HWM_SAVE_INTERVAL_SECONDS:
        save_hwm(hwm)
        _last_hwm_save = now


def send_alert(message, retries=3):
    """Send a Telegram alert with retries, since a single timed-out request
    would otherwise silently drop the alert."""
    logger.info(f"ALERT: {message}")
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return

    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": f"TRADING_BOT: {message}"},
                timeout=5,
            )
            resp.raise_for_status()
            return
        except Exception as e:
            logger.warning(f"Alert attempt {attempt}/{retries} failed: {e}")
            if attempt < retries:
                time.sleep(attempt)  # simple backoff: 1s, then 2s

    logger.error(f"Alert permanently failed after {retries} attempts: {message}")


def emergency_flatten():
    send_alert("CRITICAL: Emergency Flatten Triggered!")
    try:
        trading_client.close_all_positions(cancel_orders=True)
    except Exception as e:
        logger.error(f"Flattening failed: {e}")
    finally:
        manager_status["is_alive"] = False


def handle_symbol_error(symbol, e):
    """Track repeated failures per symbol and alert without spamming."""
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
    """Reject signals outside the configured trading window (ET), as a
    second line of defense beyond whatever gating exists in Pine."""
    now_ny = datetime.now(NY_TZ).time()
    start = datetime.strptime(TRADING_WINDOW_START, "%H:%M").time()
    end = datetime.strptime(TRADING_WINDOW_END, "%H:%M").time()
    return start <= now_ny <= end


def has_open_exposure(symbol):
    """Return True if there's already a position or an open BUY order for
    this symbol — used to reject duplicate webhook deliveries. Raises on
    API failure rather than returning False, so a check we couldn't verify
    is never silently treated as 'safe to proceed'."""
    positions = trading_client.get_all_positions()
    if any(p.symbol == symbol for p in positions):
        return True

    open_orders = trading_client.get_orders(filter=GetOrdersRequest(status=OrderStatus.OPEN))
    if any(o.symbol == symbol and o.side == OrderSide.BUY for o in open_orders):
        return True

    return False


def position_manager():
    hwm = load_hwm()
    while True:
        try:
            manager_status["last_heartbeat"] = time.time()
            manager_status["is_alive"] = True

            positions = trading_client.get_all_positions()
            # NOTE: filtering for OrderType.STOP (plain stop, market-on-trigger),
            # not STOP_LIMIT. The entry order placement (in the webhook route)
            # must also use a plain stop for the initial stop-loss, so this
            # filter actually finds it.
            open_orders = {
                o.symbol: o
                for o in trading_client.get_orders(filter=GetOrdersRequest(status=OrderStatus.OPEN))
                if o.type == OrderType.STOP
            }

            for pos in positions:
                symbol = pos.symbol
                try:
                    current, entry = float(pos.current_price), float(pos.avg_entry_price)

                    # Update high-water mark (in-memory always current; disk
                    # write throttled via maybe_save_hwm)
                    if symbol not in hwm or current > hwm[symbol]:
                        hwm[symbol] = current
                        maybe_save_hwm(hwm)

                    # 1. Breakeven move (2% gain trigger)
                    # A plain STOP order only has a stop_price (no limit_price to
                    # manage) — once triggered it becomes a market order, so this
                    # guarantees the breakeven exit fills instead of risking it
                    # sitting unfilled in a fast move.
                    if current >= (entry * 1.02) and symbol in open_orders:
                        order = open_orders[symbol]
                        if float(order.stop_price) < entry:
                            trading_client.replace_order_by_id(
                                order.id,
                                ReplaceOrderRequest(stop_price=entry),
                            )
                            logger.info(f"MOVED TO BE: {symbol}")

                    # 2. 10% Trailing Stop -> Market Order Exit
                    if current >= (entry * 1.02) and current <= (hwm[symbol] * 0.90):
                        logger.info(f"TRAILING HIT: Exiting {symbol}")

                        # Step A: cancel the existing protective stop, if present.
                        cancel_ok = True
                        if symbol in open_orders:
                            try:
                                trading_client.cancel_order_by_id(open_orders[symbol].id)
                            except Exception as e:
                                cancel_ok = False
                                send_alert(
                                    f"CRITICAL: Failed to cancel stop for {symbol} before exit — "
                                    f"possible duplicate orders on this position: {e}"
                                )

                        # Step B: submit the exit as a MARKET order. This is a
                        # crash-exit path, not a take-profit path — we need to
                        # guarantee we're flat, not guarantee a price.
                        try:
                            trading_client.close_position(symbol)
                            # Clear HWM immediately (force write) once the exit
                            # order is actually placed — this is an important
                            # state change we don't want to lose on a restart.
                            if symbol in hwm:
                                del hwm[symbol]
                                maybe_save_hwm(hwm, force=True)
                            send_alert(f"Position {symbol} closed via Trailing Stop.")
                            symbol_error_counts[symbol] = 0
                            symbol_alert_cooldown.pop(symbol, None)
                        except Exception as e:
                            # Deliberately do NOT delete hwm[symbol] here — leave
                            # the trailing condition eligible to retry next cycle.
                            send_alert(
                                f"CRITICAL: {symbol} stop was cancelled ({'ok' if cancel_ok else 'FAILED'}) "
                                f"but exit order FAILED to submit — position may be UNPROTECTED: {e}"
                            )

                except Exception as e:
                    handle_symbol_error(symbol, e)

        except Exception as e:
            if any(code in str(e) for code in ["401", "403"]):
                emergency_flatten()
                break
            logger.warning(f"Transient error: {e}. Retrying...")

        time.sleep(10)


threading.Thread(target=position_manager, daemon=True).start()


@app.route("/health", methods=["GET"])
def health():
    """Lightweight endpoint for Railway/UptimeRobot to monitor uptime."""
    return "OK", 200


@app.route("/", methods=["POST"])
def webhook():
    if not manager_status["is_alive"] or (time.time() - manager_status["last_heartbeat"] > 60):
        send_alert("REJECTED SIGNAL: Manager thread offline.")
        return "System Offline", 503

    data = request.get_json(force=True, silent=True)
    if not data:
        send_alert("REJECTED SIGNAL: No/invalid JSON body received.")
        return "Bad Request: no JSON body", 400

    # Shared secret check — this is a public URL, so anyone who finds it could
    # otherwise submit real orders on your account.
    if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
        send_alert("REJECTED SIGNAL: Invalid webhook secret.")
        return "Unauthorized", 401

    # Levels come in fresh on every alert since you trade different tickers
    # and prices daily — nothing here is hardcoded.
    try:
        symbol = str(data["symbol"]).upper()
        qty = int(data["qty"])
        buy_stop = float(data["buy_stop"])
        buy_limit = float(data["buy_limit"])
        take_profit = float(data["take_profit"])
        stop_loss = float(data["stop_loss"])
    except (KeyError, ValueError, TypeError) as e:
        send_alert(f"REJECTED SIGNAL: Malformed payload — {e}")
        return "Bad Request: malformed payload", 400

    # Sanity check the levels are in a sensible order before risking a real
    # order. Catches a bad Pine input or a fat-fingered value before it hits
    # the market.
    if not (stop_loss < buy_stop <= buy_limit < take_profit):
        send_alert(
            f"REJECTED SIGNAL: {symbol} levels out of order — "
            f"stop_loss={stop_loss}, buy_stop={buy_stop}, "
            f"buy_limit={buy_limit}, take_profit={take_profit}"
        )
        return "Bad Request: price levels out of order", 400

    # Trading window gate — second line of defense beyond Pine's own session
    # gating. Adjust via TRADING_WINDOW_START / TRADING_WINDOW_END env vars.
    if not within_trading_window():
        send_alert(f"REJECTED SIGNAL: {symbol} arrived outside trading window ({TRADING_WINDOW_START}-{TRADING_WINDOW_END} ET).")
        return "Outside trading hours", 200

    # Max position size gate — protects against a bad qty value (fat-finger,
    # bad Pine input, corrupted payload) from sizing up unreasonably.
    if qty > MAX_POSITION_SIZE:
        send_alert(f"REJECTED SIGNAL: {symbol} qty={qty} exceeds MAX_POSITION_SIZE={MAX_POSITION_SIZE}.")
        return "Qty exceeds max position size", 200

    # Everything from here through order submission runs under a single lock.
    # This closes the race where two near-simultaneous webhook deliveries
    # could both pass has_open_exposure() before either has actually
    # submitted an order — the lock makes "check, then submit" atomic.
    with order_lock:
        # Fast-path rejection: if this exact symbol was accepted within the
        # last few seconds, treat it as a duplicate delivery without even
        # hitting Alpaca. Covers the moment right after submission where
        # Alpaca's own position/order data might not have caught up yet.
        last_seen = recent_signals.get(symbol)
        if last_seen and (time.time() - last_seen) < SIGNAL_DEDUPE_WINDOW_SECONDS:
            send_alert(f"Duplicate signal ignored for {symbol} — received again within {SIGNAL_DEDUPE_WINDOW_SECONDS}s.")
            return "Duplicate (rate-limited)", 200

        # Duplicate protection — TradingView can resend the same alert
        # (retries, network hiccups). If we already hold a position or have
        # an open BUY order for this symbol, don't enter again. If the check
        # itself fails, we fail CLOSED (reject) rather than risk a silent
        # double entry.
        try:
            if has_open_exposure(symbol):
                send_alert(f"Duplicate signal ignored for {symbol} — existing position/order found.")
                return "Duplicate", 200
        except Exception as e:
            send_alert(f"CRITICAL: Could not verify duplicate protection for {symbol}, rejecting for safety: {e}")
            return "Duplicate check failed", 500

        # Buying power check — avoids submitting an order you already know
        # Alpaca will reject, and surfaces the reason clearly via alert
        # instead of just seeing an opaque Alpaca error later.
        try:
            account = trading_client.get_account()
            buying_power = float(account.buying_power)
        except Exception as e:
            send_alert(f"CRITICAL: Could not check buying power for {symbol}, rejecting for safety: {e}")
            return "Buying power check failed", 500

        required = qty * buy_limit
        if buying_power < required:
            send_alert(
                f"REJECTED SIGNAL: {symbol} insufficient buying power — "
                f"need ${required:.2f}, have ${buying_power:.2f}"
            )
            return "Insufficient buying power", 200

        try:
            order = StopLimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                stop_price=buy_stop,
                limit_price=buy_limit,
                order_class=OrderClass.BRACKET,
                take_profit=TakeProfitRequest(limit_price=take_profit),
                # stop_price only, no limit_price -> this creates a plain STOP
                # order (market-on-trigger) for the stop-loss leg, matching what
                # position_manager filters for (OrderType.STOP) and avoiding a
                # stop-limit exit that can sit unfilled in a fast drop.
                stop_loss=StopLossRequest(stop_price=stop_loss),
            )
            submitted = trading_client.submit_order(order)
            recent_signals[symbol] = time.time()
            send_alert(
                f"Entry placed: {symbol} qty={qty} stop={buy_stop} limit={buy_limit} "
                f"| TP={take_profit} SL={stop_loss}"
            )
            logger.info(f"Order submitted: {submitted.id}")
        except Exception as e:
            send_alert(f"CRITICAL: Failed to submit entry order for {symbol}: {e}")
            return "Order submission failed", 500

    return "Success", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))



























































































































