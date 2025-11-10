import os
import json
import time
import threading
from math import ceil
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify

# Alpaca SDK v3
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, ClosePositionRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ─────────────────────────────────────────────────────────────────────────────
# Environment
# ─────────────────────────────────────────────────────────────────────────────
API_KEY      = os.getenv("APCA_API_KEY_ID")
SECRET_KEY   = os.getenv("APCA_API_SECRET_KEY")
BASE_URL     = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")  # default matches your screenshots

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

PAPER = "paper" in BASE_URL

# Alpaca clients
trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data    = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# App
app = Flask(__name__)
tz_et = ZoneInfo("America/New_York")

# ─────────────────────────────────────────────────────────────────────────────
# State
# ─────────────────────────────────────────────────────────────────────────────
# Pending entries keyed by symbol
#   value: {
#     "qty": int,
#     "source": str,
#     "not_before": datetime (ET),
#     "placed": bool,
#     "entry_limit": float | None,
#     "entry_price": float | None,
#   }
pending_entries = {}

# Open trades keyed by symbol
#   value: { "entry": float, "qty": int }
open_trades = {}

# Loss counter per day per ticker
#   loss_counts[YYYYMMDD][symbol] = int
loss_counts = {}

# Track today key
def today_key():
    return datetime.now(tz=tz_et).strftime("%Y%m%d")

def reset_daily_state_if_needed():
    key = today_key()
    if key not in loss_counts:
        loss_counts.clear()
        loss_counts[key] = {}
        # keep open_trades/pending_entries; daily loss limits apply per new day

def get_live_quote(symbol: str):
    """Return (bid, ask) floats using latest quote; fall back to same price if missing."""
    try:
        q = data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
        q = q[symbol]
        bid = float(q.bid_price) if q.bid_price else None
        ask = float(q.ask_price) if q.ask_price else None
        # graceful fallback
        if bid is None and ask is not None:
            bid = ask
        if ask is None and bid is not None:
            ask = bid
        if bid is None and ask is None:
            return None, None
        return bid, ask
    except Exception as e:
        print(f"⚠️  Quote fetch failed for {symbol}: {e}", flush=True)
        return None, None

def price_buffer(p: float) -> float:
    """Dynamic buffer: ≥ $1 → $0.03 ; < $1 → $0.003"""
    if p is None:
        # default to higher tier if unknown
        return 0.03
    return 0.03 if p >= 1.0 else 0.003

def round_lim(p: float) -> float:
    """Round limit price to 4 dp to play nice with sub-dollar names."""
    return round(p, 4)

def next_bar_start(now_et: datetime) -> datetime:
    """Ceil to the next full minute (works for your 1m/5m/15m bars)."""
    return (now_et.replace(second=0, microsecond=0) + timedelta(minutes=1))

def inc_loss(symbol: str):
    key = today_key()
    d = loss_counts.setdefault(key, {})
    d[symbol] = d.get(symbol, 0) + 1

def losses_for(symbol: str) -> int:
    key = today_key()
    return loss_counts.get(key, {}).get(symbol, 0)

# ─────────────────────────────────────────────────────────────────────────────
# Background loops
# ─────────────────────────────────────────────────────────────────────────────
def entry_worker():
    """Places pending BUY entries at next bar open + buffer, as DAY limit (extended hours)."""
    while True:
        try:
            reset_daily_state_if_needed()
            now = datetime.now(tz=tz_et)
            for sym, st in list(pending_entries.items()):
                if st["placed"]:
                    continue
                if now >= st["not_before"]:
                    # get live ask and place limit above it by buffer
                    bid, ask = get_live_quote(sym)
                    ref = ask if ask is not None else bid
                    if ref is None:
                        print(f"⚠️  No quote for {sym}; retrying...", flush=True)
                        continue
                    limit_price = round_lim(ref + price_buffer(ref))
                    try:
                        order = trading.submit_order(
                            order_data=LimitOrderRequest(
                                symbol=sym,
                                qty=st["qty"],
                                side=OrderSide.BUY,
                                time_in_force=TimeInForce.DAY,
                                limit_price=limit_price,
                                extended_hours=True
                            )
                        )
                        st["placed"] = True
                        st["entry_limit"] = limit_price
                        print(f"✅ BUY placed {sym} x{st['qty']} @ {limit_price} (source={st['source']})", flush=True)
                        # track open trade
                        open_trades[sym] = {"entry": limit_price, "qty": st["qty"]}
                    except Exception as e:
                        print(f"❌ BUY submit failed {sym}: {e}", flush=True)
                        # keep retrying; if permanent, you can clear or keep attempting
            # 19:59 ET sweep every loop
            if now.hour == 19 and now.minute == 59:
                force_close_all(now)
                # small sleep to avoid spamming that minute
                time.sleep(5)
        except Exception as e:
            print(f"⚠️  entry_worker error: {e}", flush=True)
        time.sleep(1)

def chase_sell(symbol: str, qty: int, target_close: float):
    """
    Aggressive limit exit:
      1) Try at signal_close,
      2) If not likely/nearby, chase using live bid stepping a few ticks.
    """
    max_steps = 12
    step = 0
    placed = False
    last_err = None

    while step < max_steps:
        bid, ask = get_live_quote(symbol)
        if bid is None and ask is None:
            time.sleep(0.5)
            step += 1
            continue

        # Start at signal_close; if that looks stale (> top-of-book), use bid
        ref = bid if bid is not None else ask
        lim = target_close
        # If target is *above* bid (likely won’t fill immediately), start from bid,
        # then drop slightly to ensure fill.
        if ref is not None and target_close > ref:
            lim = ref  # align to current bid
        # Edge nudge down a hair to encourage fill
        nudge = 0.001 if (ref is not None and ref < 1.0) else 0.01
        limit_price = round_lim(max(0.0001, lim - nudge))

        try:
            order = trading.submit_order(
                order_data=LimitOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                    limit_price=limit_price,
                    extended_hours=True
                )
            )
            placed = True
            print(f"✅ SELL placed {symbol} x{qty} @ {limit_price}", flush=True)
            break
        except Exception as e:
            last_err = str(e)
            print(f"❌ SELL attempt {step+1}/{max_steps} failed {symbol}: {last_err}", flush=True)
            step += 1
            time.sleep(0.4)

    if not placed:
        print(f"⛔ Gave up chasing SELL for {symbol}. Last error: {last_err}", flush=True)

def rth_close_time(now_et: datetime) -> datetime:
    # 19:59 ET explicit request
    return now_et.replace(hour=19, minute=59, second=0, microsecond=0)

def force_close_all(now_et: datetime):
    """At 19:59 ET, aggressively exit any open positions via limit sells."""
    try:
        positions = trading.get_all_positions()
        if not positions:
            return
        print("🧹 19:59 ET sweep: closing open positions...", flush=True)
        for p in positions:
            sym = p.symbol
            qty = int(float(p.qty))  # qty is string in SDK object
            if qty <= 0:
                continue
            bid, ask = get_live_quote(sym)
            ref = bid if bid is not None else ask
            if ref is None:
                # fallback: use close position market-like API (still sends best-effort)
                try:
                    trading.close_position(ClosePositionRequest(symbol=sym))
                    print(f"✅ Fallback close_position for {sym}", flush=True)
                except Exception as e:
                    print(f"❌ Fallback close_position failed {sym}: {e}", flush=True)
                continue
            # Aggressive limit just below bid
            nudge = 0.001 if ref < 1.0 else 0.01
            limit_price = round_lim(max(0.0001, ref - nudge))
            try:
                trading.submit_order(
                    order_data=LimitOrderRequest(
                        symbol=sym,
                        qty=qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                        limit_price=limit_price,
                        extended_hours=True
                    )
                )
                print(f"✅ EOD SELL placed {sym} x{qty} @ {limit_price}", flush=True)
            except Exception as e:
                print(f"❌ EOD SELL failed {sym}: {e}", flush=True)
    except Exception as e:
        print(f"⚠️  force_close_all error: {e}", flush=True)

# Kick off worker thread
threading.Thread(target=entry_worker, daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})

@app.post("/tv")
def tv():
    """
    Expected JSON (examples):
    VBTS BUY:
    {
      "secret":"CHRISBOT1501",
      "action":"BUY",
      "ticker":"WORX",
      "quantity":100,
      "source":"VBTS_TEMA_BUY"
    }

    Smoothed HA BUY:
    {
      "secret":"CHRISBOT1501",
      "action":"BUY",
      "ticker":"WORX",
      "quantity":100,
      "source":"SMOOTH_HA_BUY"
    }

    Smoothed HA SELL / STOP:
    {
      "secret":"CHRISBOT1501",
      "action":"SELL",
      "ticker":"WORX",
      "quantity":100,
      "signal_close":0.2468,
      "source":"SMOOTH_HA_SELL"
    }
    """
    try:
        payload = request.get_json(force=True, silent=False)
    except Exception:
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    if not payload or payload.get("secret") != WEBHOOK_SECRET:
        print("⛔ Unauthorized webhook attempt", flush=True)
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    action   = str(payload.get("action", "")).upper()
    symbol   = str(payload.get("ticker", "")).upper()
    qty      = int(payload.get("quantity", 0))
    source   = str(payload.get("source", ""))

    if not symbol or qty <= 0:
        return jsonify({"ok": False, "error": "bad_params"}), 400

    reset_daily_state_if_needed()

    if action == "BUY":
        # Loss guard: 2 losses per ticker per day
        if losses_for(symbol) >= 2:
            msg = f"🚫 Blocked BUY for {symbol}: reached 2-loss daily limit."
            print(msg, flush=True)
            return jsonify({"ok": False, "blocked": True, "reason": "loss_limit"}), 200

        # Schedule for next bar start
        now_et = datetime.now(tz=tz_et)
        nb = next_bar_start(now_et)
        pending_entries[symbol] = {
            "qty": qty,
            "source": source or "BUY",
            "not_before": nb,
            "placed": False,
            "entry_limit": None,
            "entry_price": None,
        }
        print(f"🕒 Pending BUY set for {symbol}: qty={qty} source={source} at next bar {nb.strftime('%H:%M:%S ET')}", flush=True)
        return jsonify({"ok": True, "pending": True, "symbol": symbol, "next_bar_et": nb.isoformat()})

    elif action == "SELL":
        # SELL/STOP must close all open positions for symbol
        target_close = payload.get("signal_close", None)
        if target_close is None:
            # Not strictly required, but recommended; still chase with quotes
            target_close = 0.0

        # Cancel any pending entry for this symbol
        if symbol in pending_entries:
            del pending_entries[symbol]
            print(f"ℹ️  SELL canceled pending BUY for {symbol}", flush=True)

        # Determine qty to close (prefer position size if available)
        close_qty = qty
        try:
            pos = trading.get_open_position(symbol)
            if pos:
                close_qty = int(float(pos.qty))
        except Exception:
            pass

        if close_qty <= 0:
            print(f"ℹ️  No open position to SELL for {symbol}.", flush=True)
            return jsonify({"ok": True, "sold": False, "reason": "no_position"})

        # Compute P/L to update daily loss counter (best-effort)
        entry_info = open_trades.get(symbol)
        if entry_info:
            pnl = float(target_close) - float(entry_info["entry"])
            if pnl < 0:
                inc_loss(symbol)

        # Aggressive limit exit
        print(f"📩 SELL/STOP for {symbol} qty={close_qty} source={source} target_close={target_close}", flush=True)
        chase_sell(symbol, close_qty, float(target_close))
        # cleanup local open trade record
        if symbol in open_trades:
            del open_trades[symbol]
        return jsonify({"ok": True, "sold": True})

    else:
        return jsonify({"ok": False, "error": "unknown_action"}), 400

# ─────────────────────────────────────────────────────────────────────────────
# WSGI
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))







































































