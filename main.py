import json
import os
import threading
import time
import requests
from datetime import datetime
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

app = Flask(__name__)

# SANITY CHECK: Prints status on startup
is_live = os.getenv("LIVE_TRADING", "False") == "True"
print(f"--- BOT STARTED: LIVE_TRADING={is_live} ---")
trading_client = TradingClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"), paper=not is_live)

HWM_FILE = "hwm_data.json"
manager_status = {"last_heartbeat": time.time(), "is_alive": True}
symbol_error_counts = {}
symbol_alert_cooldown = {}  # tracks last alert count sent, to avoid spam
ERROR_ALERT_INTERVAL = 10   # once past threshold, re-alert every Nth failure


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


def send_alert(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id, "text": f"TRADING_BOT: {message}"},
                timeout=5,
            )
        except Exception as e:
            print(f"Alert failed: {e}")
    print(f"[{datetime.now()}] ALERT: {message}")


def emergency_flatten():
    send_alert("CRITICAL: Emergency Flatten Triggered!")
    try:
        trading_client.close_all_positions(cancel_orders=True)
    except Exception as e:
        print(f"Flattening failed: {e}")
    finally:
        manager_status["is_alive"] = False


def handle_symbol_error(symbol, e):
    """Track repeated failures per symbol and alert without spamming."""
    symbol_error_counts[symbol] = symbol_error_counts.get(symbol, 0) + 1
    count = symbol_error_counts[symbol]
    print(f"Error managing {symbol}: {e}")

    if count == 3:
        send_alert(f"CRITICAL: Symbol {symbol} failing repeatedly ({count} consecutive errors).")
        symbol_alert_cooldown[symbol] = count
    elif count > 3 and (count - symbol_alert_cooldown.get(symbol, 3)) >= ERROR_ALERT_INTERVAL:
        send_alert(f"CRITICAL: Symbol {symbol} still failing ({count} consecutive errors).")
        symbol_alert_cooldown[symbol] = count


def position_manager():
    hwm = load_hwm()
    while True:
        try:
            manager_status["last_heartbeat"] = time.time()
            manager_status["is_alive"] = True

            positions = trading_client.get_all_positions()
            # NOTE: filtering for OrderType.STOP (plain stop, market-on-trigger),
            # not STOP_LIMIT. The entry order placement (in the webhook route)
            # must also use a plain StopOrderRequest for the initial stop-loss,
            # so this filter actually finds it.
            open_orders = {
                o.symbol: o
                for o in trading_client.get_orders(filter=GetOrdersRequest(status=OrderStatus.OPEN))
                if o.type == OrderType.STOP
            }

            for pos in positions:
                symbol = pos.symbol
                try:
                    current, entry = float(pos.current_price), float(pos.avg_entry_price)

                    # Update high-water mark
                    if symbol not in hwm or current > hwm[symbol]:
                        hwm[symbol] = current
                        save_hwm(hwm)

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
                            print(f"MOVED TO BE: {symbol}")

                    # 2. 10% Trailing Stop -> Market Order Exit
                    if current >= (entry * 1.02) and current <= (hwm[symbol] * 0.90):
                        print(f"TRAILING HIT: Exiting {symbol}")

                        # Step A: cancel the existing protective stop, if present.
                        # Track whether this succeeded so we know if the position
                        # is temporarily unprotected.
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

                        # Step B: submit the exit as a MARKET order. This is a crash-exit
                        # path, not a take-profit path — we need to guarantee we're
                        # flat, not guarantee a price. A limit order can sit unfilled
                        # while price gaps straight through it, leaving the position
                        # open during exactly the move we're trying to escape.
                        # This is handled separately from the cancel above so a
                        # failure here is caught on its own.
                        try:
                            trading_client.close_position(symbol)
                            # Only clear HWM once the exit order is actually placed.
                            if symbol in hwm:
                                del hwm[symbol]
                                save_hwm(hwm)
                            send_alert(f"Position {symbol} closed via Trailing Stop.")
                            symbol_error_counts[symbol] = 0
                            symbol_alert_cooldown.pop(symbol, None)
                        except Exception as e:
                            # Deliberately do NOT delete hwm[symbol] here — leave the
                            # trailing condition eligible to retry next cycle. But
                            # don't wait for the 3-strike counter: this failure means
                            # a real position may be sitting with zero protection.
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
            print(f"Transient error: {e}. Retrying...")

        time.sleep(10)


threading.Thread(target=position_manager, daemon=True).start()


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
        send_alert(
            f"Entry placed: {symbol} qty={qty} stop={buy_stop} limit={buy_limit} "
            f"| TP={take_profit} SL={stop_loss}"
        )
        print(f"Order submitted: {submitted.id}")
    except Exception as e:
        send_alert(f"CRITICAL: Failed to submit entry order for {symbol}: {e}")
        return "Order submission failed", 500

    return "Success", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))



























































































































