import json
import os
import threading
import time
import requests
from datetime import datetime
from flask import Flask, request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import ReplaceOrderRequest, GetOrdersRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderStatus, OrderType

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
            open_orders = {
                o.symbol: o
                for o in trading_client.get_orders(filter=GetOrdersRequest(status=OrderStatus.OPEN))
                if o.type == OrderType.STOP_LIMIT
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
                    if current >= (entry * 1.02) and symbol in open_orders:
                        order = open_orders[symbol]
                        if float(order.stop_price) < entry:
                            trading_client.replace_order_by_id(
                                order.id,
                                ReplaceOrderRequest(stop_price=entry, limit_price=entry - 0.01),
                            )
                            print(f"MOVED TO BE: {symbol}")

                    # 2. 10% Trailing Stop -> Marketable Limit Exit
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

                        # Step B: submit the exit order. This is handled separately
                        # from the cancel so a failure here is caught on its own,
                        # since it means the position may now have NO protective
                        # order at all.
                        try:
                            trading_client.submit_order(
                                LimitOrderRequest(
                                    symbol=symbol,
                                    qty=pos.qty,
                                    side=OrderSide.SELL,
                                    limit_price=round(current * 0.98, 2),
                                    time_in_force=TimeInForce.GTC,
                                )
                            )
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
    # ... [Insert order submission logic] ...
    return "Success", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))



























































































































