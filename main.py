import json
import os
import logging
import threading
import time
from datetime import datetime, date
from typing import Dict, Any, Optional

from flask import Flask, request, jsonify

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ============================================================
# CONFIG & GLOBALS
# ============================================================

# ---- Env vars (match your Railway config exactly) ----
API_KEY = os.environ.get("APCA_API_KEY_ID")
SECRET_KEY = os.environ.get("APCA_API_SECRET_KEY")
ALPACA_BASE_URL = os.environ.get("APCA_API_BASE_URL") # library reads this
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise SystemExit("APCA_API_KEY_ID / APCA_API_SECRET_KEY not set")

# ---- Alpaca clients ----
# NOTE: we do NOT pass base_url here; paper=True + APCA_API_BASE_URL env does the job.
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# ---- Behaviour knobs ----
VERBOSE = True # always-on verbose mode

ENTRY_OFFSET = 0.01 # buys: +1 cent above your entry
STOP_OFFSET = -0.01 # stops: -1 cent below your stop level
TARGET_OFFSET = -0.01 # targets: -1 cent below your target

ENTRY_TIMEOUT_SEC = 60 # 1 minute to get filled, else cancel
TRAIL_PCT = 0.15 # 15% trailing stop from highest price
POLL_INTERVAL_SEC = 2 # how often watcher checks price

# trade state (per symbol)
open_trades: Dict[str, Dict[str, Any]] = {}
daily_stats: Dict[date, Dict[str, Any]] = {}

# Flask app
app = Flask(__name__)

# ---- Logging ----
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("level-bot")


# ============================================================
# HELPER FUNCTIONS
# ============================================================

def v(msg: str, *args):
    """Verbose logger."""
    if VERBOSE:
        log.info(msg, *args)


def get_latest_price(symbol: str) -> Optional[float]:
    """
    Get a representative current price for the symbol from Alpaca.
    Uses latest quote bid/ask midpoint, falls back to bid, then ask.
    """
    try:
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol)
        resp = data_client.get_stock_latest_quote(req)
        quote = resp[symbol]

        bid = float(quote.bid_price or 0)
        ask = float(quote.ask_price or 0)

        if bid > 0 and ask > 0:
            price = (bid + ask) / 2.0
            v("PRICE %s: bid=%.4f ask=%.4f mid=%.4f", symbol, bid, ask, price)
            return price
        elif bid > 0:
            v("PRICE %s: bid=%.4f (ask missing)", symbol, bid)
            return bid
        elif ask > 0:
            v("PRICE %s: ask=%.4f (bid missing)", symbol, ask)
            return ask
        else:
            log.warning("⚠️ PRICE %s: both bid/ask missing -> cannot compute price", symbol)
            return None
    except Exception as e:
        log.error("❌ Failed to get latest quote for %s: %s", symbol, e)
        return None


def cancel_open_orders_for_symbol(symbol: str):
    """
    Cancel all open orders for the given symbol and log why.
    """
    try:
        orders = trading_client.get_open_orders()
        cancelled = 0
        for o in orders:
            if o.symbol == symbol:
                trading_client.cancel_order_by_id(o.id)
                cancelled += 1
        v("CANCEL %s: cancelled %d open orders", symbol, cancelled)
    except Exception as e:
        log.error("❌ Failed to cancel open orders for %s: %s", symbol, e)


def get_position_qty(symbol: str) -> float:
    """
    Return current open qty for symbol, or 0 if none.
    """
    try:
        pos = trading_client.get_open_position(symbol)
        qty = float(pos.qty)
        v("POS %s: open qty=%.2f", symbol, qty)
        return qty
    except Exception:
        v("POS %s: no open position (404)", symbol)
        return 0.0


def place_limit_order(
    symbol: str,
    qty: int,
    side: OrderSide,
    limit_price: float,
    reason: str,
    tif: TimeInForce = TimeInForce.DAY,
):
    """
    Place a generic limit order and log details.
    """
    order_req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=tif,
        limit_price=limit_price,
    )
    order = trading_client.submit_order(order_req)
    log.info("✅ ORDER %s %s x%d @ %.4f (reason=%s)", side.name, symbol, qty, limit_price, reason)
    return order


def update_daily_stats(symbol: str, pnl: float, pnl_pct: float):
    """
    Track win/loss and net P&L per calendar day.
    """
    today = date.today()
    stats = daily_stats.setdefault(today, {"trades": 0, "wins": 0, "losses": 0, "net": 0.0})

    stats["trades"] += 1
    stats["net"] += pnl
    if pnl >= 0:
        stats["wins"] += 1
    else:
        stats["losses"] += 1

    log.info(
        "📊 PNL %s: $%.2f (%.2f%%) | trades=%d wins=%d losses=%d net=$%.2f",
        symbol, pnl, pnl_pct,
        stats["trades"], stats["wins"], stats["losses"], stats["net"],
    )


def compute_trailing_stop(highest: float, trail_pct: float) -> float:
    """
    For a long: trailing stop level from highest price.
    """
    return highest * (1.0 - trail_pct)


# ============================================================
# TRADE WATCHER
# ============================================================

def trade_watcher(symbol: str):
    """
    Background thread that manages:
      - entry timeout
      - stop hit
      - target hit
      - trailing stop
      - marking CLOSED + PnL
    """
    v("WATCHER %s: started watcher thread", symbol)
    trade = open_trades.get(symbol)
    if not trade:
        log.warning("WATCHER %s: missing trade dict, exiting watcher", symbol)
        return

    entry_submitted_at = trade["entry_submitted_at"]
    entry_level = trade["entry_level"]
    stop_level = trade["stop_level"]
    target_level = trade["target_level"]
    qty = trade["qty"]

    side = trade["side"] # currently LONG only

    highest_price = None
    exit_done = False

    while True:
        try:
            # Small sleep between polls
            time.sleep(POLL_INTERVAL_SEC)

            # If trade removed externally, stop watcher
            if symbol not in open_trades:
                v("WATCHER %s: trade removed from memory, exiting", symbol)
                return

            trade = open_trades[symbol]
            status = trade["status"]

            # ---------------------------------------------------
            # 1) Entry phase: WAITING_ENTRY
            # ---------------------------------------------------
            if status == "WAITING_ENTRY":
                v("WATCHER %s: status=WAITING_ENTRY, checking entry fill / timeout", symbol)

                now = datetime.utcnow()
                elapsed = (now - entry_submitted_at).total_seconds()

                pos_qty = get_position_qty(symbol)
                if pos_qty >= qty:
                    # Entry filled
                    trade["status"] = "OPEN"
                    trade["opened_at"] = now.isoformat()
                    v("WATCHER %s: ENTRY filled, status -> OPEN (qty=%.2f)", symbol, pos_qty)
                    highest_price = None # reset for trailing
                    continue

                # If not filled and timeout exceeded → cancel & mark cancelled
                if elapsed >= ENTRY_TIMEOUT_SEC:
                    log.warning(
                        "⏰ WATCHER %s: ENTRY not filled after %.1fs, cancelling orders & marking CANCELED",
                        symbol, elapsed,
                    )
                    cancel_open_orders_for_symbol(symbol)
                    trade["status"] = "CANCELED"
                    trade["exit_reason"] = "ENTRY_TIMEOUT"
                    return

                v(
                    "WATCHER %s: ENTRY still pending (elapsed=%.1fs < %.1fs)",
                    symbol, elapsed, ENTRY_TIMEOUT_SEC,
                )
                continue

            # ---------------------------------------------------
            # 2) Already CLOSED / CANCELED
            # ---------------------------------------------------
            if status in ("CLOSED", "CANCELED"):
                v("WATCHER %s: status=%s, stopping watcher", symbol, status)
                return

            # ---------------------------------------------------
            # 3) OPEN phase: manage stop / target / trailing
            # ---------------------------------------------------
            v("WATCHER %s: status=OPEN, checking stop/target/trail", symbol)

            # Ensure we still actually have a position
            pos_qty = get_position_qty(symbol)
            if pos_qty <= 0:
                # No position but status OPEN → mark CLOSED (unknown exit)
                log.warning("WATCHER %s: no position but status=OPEN, marking CLOSED (unknown exit)", symbol)
                trade["status"] = "CLOSED"
                trade.setdefault("exit_reason", "UNKNOWN_EXIT")
                # We can't compute exact PnL without exit price; skip PnL here.
                return

            # Use latest Alpaca price
            price = get_latest_price(symbol)
            if price is None:
                v("WATCHER %s: price unavailable, skipping this cycle", symbol)
                continue

            # Track highest price since open (for trailing)
            if highest_price is None:
                highest_price = price
                v("WATCHER %s: initial highest=%.4f", symbol, highest_price)
            else:
                if price > highest_price:
                    v("WATCHER %s: new high %.4f (old high %.4f)", symbol, price, highest_price)
                    highest_price = price

            # Compute trailing stop level from highest
            trail_level = compute_trailing_stop(highest_price, TRAIL_PCT)
            v(
                "WATCHER %s: price=%.4f | stop=%.4f | target=%.4f | trail=%.4f (15%% below high=%.4f)",
                symbol, price, stop_level, target_level, trail_level, highest_price
            )

            # PRIORITY of exits:
            # 1) Hard stop
            # 2) Target
            # 3) Trailing stop

            # --- Hard stop hit? ---
            if price <= stop_level and not exit_done:
                stop_limit = round(stop_level + STOP_OFFSET, 4)
                v(
                    "WATCHER %s: STOP HIT (price=%.4f <= stop=%.4f) -> placing STOP EXIT @ %.4f",
                    symbol, price, stop_level, stop_limit
                )
                place_limit_order(symbol, int(pos_qty), OrderSide.SELL, stop_limit, reason="STOP_HIT")
                exit_done = True
                trade["exit_reason"] = "STOP"
                trade["exit_price_est"] = stop_limit
                # We'll mark CLOSED & PnL when position qty becomes 0
                continue

            # --- Target hit? ---
            if price >= target_level and not exit_done:
                target_limit = round(target_level + TARGET_OFFSET, 4)
                v(
                    "WATCHER %s: TARGET HIT (price=%.4f >= target=%.4f) -> placing TARGET EXIT @ %.4f",
                    symbol, price, target_level, target_limit
                )
                place_limit_order(symbol, int(pos_qty), OrderSide.SELL, target_limit, reason="TARGET_HIT")
                exit_done = True
                trade["exit_reason"] = "TARGET"
                trade["exit_price_est"] = target_limit
                continue

            # --- Trailing stop (only if in profit vs entry) ---
            entry_level_local = trade["entry_level"]
            if price > entry_level_local:
                trail_level_now = trail_level
                if price <= trail_level_now and not exit_done:
                    trail_limit = round(trail_level_now + STOP_OFFSET, 4)
                    v(
                        "WATCHER %s: TRAIL HIT (price=%.4f <= trail=%.4f) -> placing TRAIL EXIT @ %.4f",
                        symbol, price, trail_level_now, trail_limit
                    )
                    place_limit_order(symbol, int(pos_qty), OrderSide.SELL, trail_limit, reason="TRAIL_HIT")
                    exit_done = True
                    trade["exit_reason"] = "TRAIL"
                    trade["exit_price_est"] = trail_limit
                    continue
            else:
                v(
                    "WATCHER %s: trailing inactive (price=%.4f <= entry=%.4f), waiting for profit first",
                    symbol, price, entry_level_local
                )

            # ---------------------------------------------------
            # 4) Check if position is now flat (after exit orders)
            # ---------------------------------------------------
            if exit_done:
                flat_qty = get_position_qty(symbol)
                if flat_qty <= 0:
                    # Position closed -> compute PnL
                    exit_price = trade.get("exit_price_est", price)
                    entry_price = trade["entry_level"]
                    pnl = (exit_price - entry_price) * qty
                    pnl_pct = (exit_price / entry_price - 1.0) * 100.0
                    log.info(
                        "✅ TRADE CLOSED %s: reason=%s entry=%.4f exit≈%.4f qty=%d PnL=$%.2f (%.2f%%)",
                        symbol,
                        trade.get("exit_reason"),
                        entry_price,
                        exit_price,
                        qty,
                        pnl,
                        pnl_pct,
                    )
                    trade["status"] = "CLOSED"
                    trade["closed_at"] = datetime.utcnow().isoformat()
                    trade["pnl"] = pnl
                    trade["pnl_pct"] = pnl_pct

                    update_daily_stats(symbol, pnl, pnl_pct)
                    return
                else:
                    v("WATCHER %s: exit orders sent but qty=%.2f still open, waiting...", symbol, flat_qty)

        except Exception as e:
            log.error("❌ WATCHER %s: unexpected error: %s", symbol, e)


# ============================================================
# WEBHOOK VALIDATION
# ============================================================

def validate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Validate and normalize TradingView payload for level-based trade.
    Required fields:
      - secret
      - ticker
      - quantity
      - entry
      - stop
      - target
    """
    v("PAYLOAD RAW: %s", payload)

    secret = payload.get("secret")
    if secret != WEBHOOK_SECRET:
        raise ValueError("Invalid secret")

    ticker = payload.get("ticker")
    if not ticker or not isinstance(ticker, str):
        raise ValueError("Missing or invalid 'ticker'")

    # quantity can be 'quantity' or 'qty'
    qty = payload.get("quantity", payload.get("qty"))
    try:
        qty = int(qty)
    except Exception:
        raise ValueError("Missing or invalid 'quantity'")
    if qty <= 0:
        raise ValueError("Quantity must be > 0")

    def get_level(name: str) -> float:
        val = payload.get(name)
        if val is None:
            raise ValueError(f"Missing '{name}'")
        try:
            f = float(val)
        except Exception:
            raise ValueError(f"Invalid '{name}' (not a number)")
        if f <= 0:
            raise ValueError(f"'{name}' must be > 0")
        return f

    entry_level = get_level("entry")
    stop_level = get_level("stop")
    target_level = get_level("target")

    if stop_level >= entry_level:
        raise ValueError("stop must be < entry for a long")
    if target_level <= entry_level:
        raise ValueError("target must be > entry for a long")

    action = payload.get("action", "LEVEL")
    source = payload.get("source", "LevelBot")
    side = payload.get("side", "LONG").upper()

    if side not in ("LONG",):
        raise ValueError("Only LONG side supported in this version")

    normalized = {
        "action": action,
        "source": source,
        "ticker": ticker.upper(),
        "qty": qty,
        "entry_level": float(entry_level),
        "stop_level": float(stop_level),
        "target_level": float(target_level),
        "side": side,
    }

    v(
        "PAYLOAD VALIDATED: %s qty=%d entry=%.4f stop=%.4f target=%.4f side=%s src=%s",
        normalized["ticker"], qty, entry_level, stop_level, target_level, side, source
    )
    return normalized


# ============================================================
# MAIN WEBHOOK ROUTE
# ============================================================

@app.route("/tv", methods=["POST"])
def tv_webhook():
    # 1) Parse JSON (with loud error if bad)
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception as e:
        log.error("❌ Failed to parse JSON from TradingView: %s", e)
        return jsonify({"status": "error", "message": "invalid json"}), 400

    if not isinstance(payload, dict):
        log.error("❌ Payload is not a JSON object: %r", payload)
        return jsonify({"status": "error", "message": "payload must be JSON object"}), 400

    # 2) Validate payload
    try:
        data = validate_payload(payload)
    except ValueError as ve:
        log.error("⚠️ Invalid payload, trade skipped: %s", ve)
        return jsonify({"status": "error", "message": str(ve)}), 400

    ticker = data["ticker"]
    qty = data["qty"]
    entry_level = data["entry_level"]
    stop_level = data["stop_level"]
    target_level = data["target_level"]
    side_str = data["side"]
    source = data["source"]
    action = data["action"]

    log.info(
        "➡️ NEW LEVEL REQUEST: %s action=%s qty=%d entry=%.4f stop=%.4f target=%.4f src=%s",
        ticker, action, qty, entry_level, stop_level, target_level, source
    )

    # 3) Reject if we already have an active trade for this ticker
    existing = open_trades.get(ticker)
    if existing and existing.get("status") in ("WAITING_ENTRY", "OPEN"):
        log.warning(
            "⏭️ SKIP %s: existing trade status=%s, no new trade started",
            ticker, existing.get("status"),
        )
        return jsonify({"status": "skipped", "reason": "already_in_trade"}), 200

    # 4) Compute entry limit (entry + 1 cent)
    raw_price = get_latest_price(ticker)
    if raw_price is None:
        log.warning(
            "⚠️ No live price available for %s; still placing entry limit based on your entry level",
            ticker,
        )

    entry_limit = round(entry_level + ENTRY_OFFSET, 4)
    v(
        "ENTRY %s: entry_level=%.4f + offset=%.4f -> limit=%.4f",
        ticker, entry_level, ENTRY_OFFSET, entry_limit
    )

    # 5) Place limit BUY (LONG only)
    side = OrderSide.BUY # current version is long-only
    try:
        place_limit_order(
            symbol=ticker,
            qty=qty,
            side=side,
            limit_price=entry_limit,
            reason=f"LEVEL_ENTRY from {source}",
            tif=TimeInForce.DAY,
        )
    except Exception as e:
        log.error("❌ Failed to submit ENTRY order for %s: %s", ticker, e)
        return jsonify({"status": "error", "message": "entry_order_failed"}), 500

    # 6) Register trade in memory
    open_trades[ticker] = {
        "symbol": ticker,
        "qty": qty,
        "side": side_str,
        "entry_level": entry_level,
        "stop_level": stop_level,
        "target_level": target_level,
        "entry_limit": entry_limit,
        "entry_submitted_at": datetime.utcnow(),
        "status": "WAITING_ENTRY",
        "source": source,
        "exit_reason": None,
        "exit_price_est": None,
    }

    v("STATE %s: created WAITING_ENTRY trade dict: %s", ticker, open_trades[ticker])

    # 7) Start watcher thread
    t = threading.Thread(target=trade_watcher, args=(ticker,), daemon=True)
    t.start()
    v("WATCHER %s: thread started", ticker)

    return jsonify({"status": "ok", "ticker": ticker, "mode": "level", "entry_limit": entry_limit}), 200


# ============================================================
# ROOT / HEALTH ROUTES (OPTIONAL)
# ============================================================

@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "message": "Level bot is running"})


@app.route("/open_trades", methods=["GET"])
def debug_open_trades():
    # helpful for debugging in browser
    return jsonify(open_trades)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    log.info("Starting Level Bot on port %d", port)
    app.run(host="0.0.0.0", port=port)




























































































