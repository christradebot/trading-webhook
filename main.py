import os, time, threading, logging
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ───────────────────────────────────────────────
# CONFIG / LOGGING
# ───────────────────────────────────────────────
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

PAPER = True
app = Flask(__name__)

logging.basicConfig(
    filename="tradebot.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s"
)
log = logging.getLogger()

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ───────────────────────────────────────────────
# GLOBAL STATE
# ───────────────────────────────────────────────
active_positions = {}
loss_tracker = {}
MAX_LOSSES_PER_TICKER = 2


def log_event(event, symbol, price, msg):
    out = f"[{event}] {symbol} @ {price} → {msg}"
    print(out)
    log.info(out)


# ───────────────────────────────────────────────
# ORDER EXECUTION HELPERS
# ───────────────────────────────────────────────
def execute_buy(symbol, qty, signal_close, source):
    """Immediate limit BUY within same candle; no waiting."""
    try:
        buffer = 0.003 if signal_close < 1 else 0.03
        target = round(signal_close + buffer, 4)

        try:
            quote = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
            live_ask = float(quote[symbol].ask_price)
            limit_price = max(target, live_ask)
        except Exception:
            limit_price = target

        order = trading_client.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                extended_hours=True,
            )
        )
        active_positions[symbol] = {"entry": limit_price, "qty": qty}
        log_event("BUY", symbol, limit_price, f"{source} (instant)")
        return order

    except Exception as e:
        log_event("ERROR", symbol, signal_close, f"BUY failed: {e}")
        return None


def execute_sell(symbol, qty, signal_close, source):
    """Limit SELL at signal close → short chase → force close."""
    try:
        limit_price = round(signal_close, 4)
        order = trading_client.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
                extended_hours=True,
            )
        )
        log_event("SELL", symbol, limit_price, f"{source} (initial)")

        # Wait ~1 bar for fill
        time.sleep(60)

        # Check open position
        try:
            pos = trading_client.get_open_position(symbol)
        except Exception:
            pos = None

        if pos:
            # one gentle chase only (-0.5%)
            new_limit = round(limit_price * 0.995, 4)
            order = trading_client.submit_order(
                LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    limit_price=new_limit,
                    extended_hours=True,
                )
            )
            log_event("CHASE", symbol, new_limit, f"{source} (2nd try)")
            time.sleep(20)
            # final forced close
            try:
                trading_client.close_position(symbol)
                log_event("EXIT", symbol, new_limit, "force close after chase")
            except Exception as e:
                log_event("FAIL", symbol, new_limit, f"final close failed: {e}")

        active_positions.pop(symbol, None)
        return order

    except Exception as e:
        log_event("ERROR", symbol, signal_close, f"SELL failed: {e}")
        return None


# ───────────────────────────────────────────────
# DAILY SHUTDOWN @19:59 ET
# ───────────────────────────────────────────────
def auto_close_positions():
    while True:
        now = datetime.now(timezone.utc)
        if now.hour == 23 and now.minute == 59:  # 19:59 ET
            try:
                positions = trading_client.get_all_positions()
                for p in positions:
                    trading_client.close_position(p.symbol)
                    log_event("AUTO-CLOSE", p.symbol, p.current_price, "End-of-day exit")
            except Exception as e:
                log_event("ERROR", "ALL", 0, f"AUTO-CLOSE failed: {e}")
            time.sleep(60)
        time.sleep(30)


threading.Thread(target=auto_close_positions, daemon=True).start()

# ───────────────────────────────────────────────
# WEBHOOK ENDPOINT
# ───────────────────────────────────────────────
@app.route("/tv", methods=["POST"])
def tv():
    if request.content_type != "application/json":
        return jsonify({"error": "Unsupported content type"}), 415

    payload = request.json
    secret = payload.get("secret")
    action = payload.get("action")
    symbol = payload.get("ticker")
    qty = int(payload.get("quantity", 0))
    signal_close = float(payload.get("signal_close", 0.0))
    source = payload.get("source", "Unknown")

    if secret != "CHRISBOT1501":
        log_event("DENIED", symbol, 0, "Invalid secret")
        return jsonify({"error": "Unauthorized"}), 403

    # max 2 losses per ticker
    if loss_tracker.get(symbol, 0) >= MAX_LOSSES_PER_TICKER:
        log_event("LOCKED", symbol, 0, "Max losses reached")
        return jsonify({"status": "locked"}), 200

    if action == "BUY":
        order = execute_buy(symbol, qty, signal_close, source)
    elif action == "SELL":
        order = execute_sell(symbol, qty, signal_close, source)
    else:
        log_event("IGNORED", symbol, 0, f"Unknown action {action}")
        return jsonify({"error": "unknown action"}), 400

    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)













































































