import os
import threading
import time
import logging
from datetime import datetime, timedelta, timezone

from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.models import Order

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# =========================================================
# CONFIG  – matches Railway variables exactly
# =========================================================

APCA_API_BASE_URL = os.environ.get("APCA_API_BASE_URL")
APCA_API_KEY_ID = os.environ.get("APCA_API_KEY_ID")
APCA_API_SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
    raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set")

# Trading client – DO NOT pass base_url (Alpaca v2 doesn’t accept it)
trading_client = TradingClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY)

# Data client (for quotes)
data_client = StockHistoricalDataClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY)

# =========================================================
# BOT SETTINGS – tweak here only if you want to
# =========================================================

# Entry limit buffer (in dollars) on top of best ask/entry level.
# You previously converged to ~1 cent for >$1 and 0.001 for sub-$1.
ENTRY_BUFFER_HIGH = 0.01     # symbol price >= 1.00
ENTRY_BUFFER_LOW = 0.001     # symbol price < 1.00

# How long an entry order is allowed to sit before cancel (seconds)
ENTRY_LIFETIME_SEC = 60

# Trailing stop percentage from the **high-water** price
TRAILING_STOP_PCT = 0.15  # 15%

# To guarantee trailing stop still exits above entry, only activate
# once price has moved at least +17.65% (≈ 1 / (1 - 0.15) - 1)
TRAIL_ACTIVATION_MULTIPLIER = 1.18  # ~+18% before trail turns on

# Polling intervals
MANAGER_SLEEP_SEC = 1.0

# =========================================================
# STATE
# =========================================================

app = Flask(__name__)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

# One dict entry per ticker
open_trades = {}
open_trades_lock = threading.Lock()

# Daily P&L
daily_stats = {
    "date": datetime.now(timezone.utc).date(),
    "trades": 0,
    "wins": 0,
    "losses": 0,
    "pnl": 0.0,
}

# =========================================================
# UTILS
# =========================================================

def _reset_daily_stats_if_new_day():
    global daily_stats
    today = datetime.now(timezone.utc).date()
    if daily_stats["date"] != today:
        logging.info("📆 New day – resetting daily P&L summary")
        daily_stats = {
            "date": today,
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "pnl": 0.0,
        }


def _get_latest_ask(symbol: str) -> float | None:
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        quote = data_client.get_stock_latest_quote(req)
        q = quote[symbol]
        # Prefer ask; fall back to bid / mid if needed
        if q.ask_price and q.ask_price > 0:
            return float(q.ask_price)
        if q.bid_price and q.bid_price > 0:
            return float(q.bid_price)
        if q.ask_price and q.bid_price:
            return float((q.ask_price + q.bid_price) / 2.0)
        return None
    except Exception as e:
        logging.error(f"❌ Failed to fetch latest quote for {symbol}: {e}")
        return None


def _place_limit_buy(symbol: str, qty: int, entry_price: float) -> Order | None:
    """
    Place an aggressive-but-bounded limit BUY that should fill quickly,
    but is cancelled after ENTRY_LIFETIME_SEC if not filled.
    """
    latest_ask = _get_latest_ask(symbol)
    if latest_ask is None:
        logging.warning(f"⚠️ No quote for {symbol}, using entry_price as limit")
        limit_price = entry_price
    else:
        # Apply buffer depending on price level
        if latest_ask >= 1.0:
            limit_price = max(entry_price, latest_ask + ENTRY_BUFFER_HIGH)
        else:
            limit_price = max(entry_price, latest_ask + ENTRY_BUFFER_LOW)

    try:
        order_req = LimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            limit_price=round(limit_price, 4),
        )
        order = trading_client.submit_order(order_req)
        logging.info(
            f"✅ ENTRY submitted {symbol} x{qty} @ {order.limit_price} "
            f"(entry level={entry_price}, ask={latest_ask})"
        )
        return order
    except Exception as e:
        logging.error(f"❌ Failed to submit BUY for {symbol}: {e}")
        return None


def _place_market_sell(symbol: str, qty: int, reason: str) -> tuple[float | None, str]:
    """
    Close position via market sell. Returns (avg_fill_price, status).
    """
    try:
        order_req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = trading_client.submit_order(order_req)
        logging.info(f"✅ EXIT submitted {symbol} x{qty} via MARKET ({reason})")

        # Poll until filled or timeout (basic)
        deadline = time.time() + 30
        while time.time() < deadline:
            filled = trading_client.get_order_by_id(order.id)
            if filled.status == "filled":
                price = float(filled.filled_avg_price)
                logging.info(
                    f"🏁 EXIT filled {symbol} x{qty} @ {price} ({reason})"
                )
                return price, "filled"
            time.sleep(0.5)

        logging.warning(
            f"⚠️ EXIT for {symbol} not confirmed filled within timeout ({reason})"
        )
        return None, "unknown"
    except Exception as e:
        logging.error(f"❌ Failed to submit EXIT for {symbol}: {e}")
        return None, "error"


def _update_pnl(symbol: str, entry_price: float, exit_price: float, qty: int):
    global daily_stats
    _reset_daily_stats_if_new_day()
    pnl = (exit_price - entry_price) * qty
    daily_stats["trades"] += 1
    if pnl >= 0:
        daily_stats["wins"] += 1
    else:
        daily_stats["losses"] += 1
    daily_stats["pnl"] += pnl

    pct = (exit_price / entry_price - 1.0) * 100.0
    logging.info(
        f"📊 PNL {symbol}: qty={qty} entry={entry_price:.4f} "
        f"exit={exit_price:.4f} -> ${pnl:.2f} ({pct:.2f}%)"
    )
    logging.info(
        f"📈 DAILY SUMMARY {daily_stats['date']}: "
        f"trades={daily_stats['trades']} "
        f"wins={daily_stats['wins']} "
        f"losses={daily_stats['losses']} "
        f"net=${daily_stats['pnl']:.2f}"
    )


# =========================================================
# TRADE MANAGER LOOP
# =========================================================

def trade_manager_loop():
    """
    Background loop:
      - Watches pending entry orders and cancels after 60s if unfilled.
      - Watches open trades for:
          * hard stop hit
          * target hit
          * 15% trailing stop (after activation level)
    """
    while True:
        try:
            _reset_daily_stats_if_new_day()
            now = datetime.now(timezone.utc)

            with open_trades_lock:
                symbols = list(open_trades.keys())

            for symbol in symbols:
                with open_trades_lock:
                    trade = open_trades.get(symbol)
                if not trade:
                    continue

                status = trade["status"]

                # 1) PENDING ENTRY – check order status / expiry
                if status == "PENDING_ENTRY":
                    order_id = trade["entry_order_id"]
                    created_at = trade["entry_created_at"]
                    try:
                        order = trading_client.get_order_by_id(order_id)
                    except Exception as e:
                        logging.error(f"❌ Failed to fetch entry order {order_id} for {symbol}: {e}")
                        continue

                    if order.status == "filled":
                        entry_price = float(order.filled_avg_price)
                        qty = int(order.filled_qty)
                        logging.info(
                            f"✅ ENTRY filled {symbol} x{qty} @ {entry_price}"
                        )
                        with open_trades_lock:
                            trade["status"] = "OPEN"
                            trade["qty"] = qty
                            trade["entry_price"] = entry_price
                            trade["high_water"] = entry_price
                            trade["trail_active"] = False
                            # Activation level so 15% trail never goes below entry
                            trade["trail_activation_price"] = entry_price * TRAIL_ACTIVATION_MULTIPLIER
                            open_trades[symbol] = trade
                        continue

                    # Cancel if lifetime exceeded
                    if now - created_at > timedelta(seconds=ENTRY_LIFETIME_SEC):
                        try:
                            trading_client.cancel_order_by_id(order_id)
                            logging.info(
                                f"⌛ ENTRY for {symbol} expired after {ENTRY_LIFETIME_SEC}s – cancelled"
                            )
                        except Exception as e:
                            logging.error(f"❌ Failed to cancel expired entry for {symbol}: {e}")
                        with open_trades_lock:
                            open_trades.pop(symbol, None)
                        continue

                # 2) OPEN TRADE – manage exits (stop, target, trailing)
                if status == "OPEN":
                    qty = trade["qty"]
                    entry_price = trade["entry_price"]
                    stop_price = trade["stop_price"]
                    target_price = trade["target_price"]
                    high_water = trade["high_water"]
                    trail_active = trade["trail_active"]
                    trail_activation_price = trade["trail_activation_price"]

                    # Get latest price (ask / mid)
                    last_price = _get_latest_ask(symbol)
                    if last_price is None:
                        continue

                    # Update high-water mark
                    if last_price > high_water:
                        high_water = last_price
                        trade["high_water"] = high_water

                    # Hard stop – ALWAYS ON
                    if last_price <= stop_price:
                        exit_price, status = _place_market_sell(
                            symbol, qty, reason="HARD_STOP"
                        )
                        if exit_price is not None:
                            _update_pnl(symbol, entry_price, exit_price, qty)
                        with open_trades_lock:
                            open_trades.pop(symbol, None)
                        continue

                    # Target hit
                    if last_price >= target_price:
                        exit_price, status = _place_market_sell(
                            symbol, qty, reason="TARGET_HIT"
                        )
                        if exit_price is not None:
                            _update_pnl(symbol, entry_price, exit_price, qty)
                        with open_trades_lock:
                            open_trades.pop(symbol, None)
                        continue

                    # Activate trailing once we move far enough
                    if not trail_active and last_price >= trail_activation_price:
                        trail_active = True
                        trade["trail_active"] = True
                        logging.info(
                            f"🎯 TRAIL activated for {symbol} at {last_price:.4f} "
                            f"(entry={entry_price:.4f}, activation={trail_activation_price:.4f})"
                        )

                    # Trailing stop logic – 15% from high-water, but never below entry/stop
                    if trail_active:
                        trail_floor = high_water * (1.0 - TRAILING_STOP_PCT)
                        trail_floor = max(trail_floor, entry_price, stop_price)

                        if last_price <= trail_floor:
                            exit_price, status = _place_market_sell(
                                symbol, qty, reason="TRAIL_STOP"
                            )
                            if exit_price is not None:
                                _update_pnl(symbol, entry_price, exit_price, qty)
                            with open_trades_lock:
                                open_trades.pop(symbol, None)
                            continue

        except Exception as e:
            logging.error(f"❌ Error in trade_manager_loop: {e}")

        time.sleep(MANAGER_SLEEP_SEC)


# Kick off background manager
manager_thread = threading.Thread(target=trade_manager_loop, daemon=True)
manager_thread.start()

# =========================================================
# WEBHOOK ENDPOINT
# =========================================================

@app.route("/tv", methods=["POST"])
def tv_webhook():
    """
    TradingView webhook endpoint.
    Expects JSON PLAN payload of the form:

    {
      "secret": "CHRISBOT1501",
      "action": "PLAN",
      "ticker": "BCG",
      "quantity": 100,
      "entry_price": 2.03,
      "stop_price": 1.93,
      "target_price": 2.68,
      "source": "3M-midpoint"
    }
    """

    # --- Parse JSON
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception as e:
        logging.error(f"❌ Failed to parse JSON from TradingView: {e}")
        return jsonify({"status": "error", "message": "invalid_json"}), 400

    logging.info(f"RAW PAYLOAD: {payload}")

    # --- Secret check
    if payload.get("secret") != WEBHOOK_SECRET:
        logging.warning("🚫 Invalid secret in payload")
        return jsonify({"status": "error", "message": "unauthorized"}), 401

    action = str(payload.get("action", "")).upper()

    if action != "PLAN":
        logging.warning(f"⚠️ Unsupported action '{action}' – expected 'PLAN'")
        return jsonify({"status": "error", "message": "unsupported_action"}), 400

    # --- Extract plan fields
    try:
        symbol = str(payload["ticker"]).upper()
        qty = int(payload["quantity"])
        entry_price = float(payload["entry_price"])
        stop_price = float(payload["stop_price"])
        target_price = float(payload["target_price"])
        source = str(payload.get("source", "LEVEL_PLAN"))
    except Exception as e:
        logging.error(f"❌ Missing or invalid PLAN fields: {e}")
        return jsonify({"status": "error", "message": "bad_plan_fields"}), 400

    if qty <= 0:
        return jsonify({"status": "error", "message": "quantity_must_be_positive"}), 400
    if not (0 < stop_price < entry_price < target_price):
        return jsonify({"status": "error", "message": "invalid_price_hierarchy"}), 400

    # --- Check if we already have an active trade for this ticker
    with open_trades_lock:
        existing = open_trades.get(symbol)
        if existing and existing["status"] in ("PENDING_ENTRY", "OPEN"):
            logging.warning(
                f"⏭️ PLAN skipped for {symbol}; existing trade status={existing['status']}"
            )
            return jsonify({"status": "skipped", "message": "trade_already_open"}), 200

    # --- Place entry order immediately (price has crossed alert level)
    entry_order = _place_limit_buy(symbol, qty, entry_price)
    if not entry_order:
        return jsonify({"status": "error", "message": "failed_to_place_entry"}), 500

    # --- Register new trade in state
    with open_trades_lock:
        open_trades[symbol] = {
            "symbol": symbol,
            "status": "PENDING_ENTRY",
            "qty": qty,
            "entry_price": entry_price,     # provisional; overwritten on fill
            "stop_price": stop_price,
            "target_price": target_price,
            "source": source,
            "entry_order_id": entry_order.id,
            "entry_created_at": datetime.now(timezone.utc),
            "high_water": entry_price,
            "trail_active": False,
            "trail_activation_price": entry_price * TRAIL_ACTIVATION_MULTIPLIER,
        }

    logging.info(
        f"📥 PLAN accepted for {symbol}: qty={qty} "
        f"entry={entry_price} stop={stop_price} target={target_price} src={source}"
    )

    return jsonify({"status": "ok", "message": "plan_accepted"}), 200


# =========================================================
# WSGI entrypoint
# =========================================================

if __name__ == "__main__":
    # Local testing only – Railway will use gunicorn
    app.run(host="0.0.0.0", port=8080, debug=False)




























































































