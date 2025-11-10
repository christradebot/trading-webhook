import os
import json
import time
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ──────────────────────────────────────────────
# ENVIRONMENT VARIABLES
# ──────────────────────────────────────────────
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

PAPER = "paper" in BASE_URL
trading = TradingClient(API_KEY, SECRET_KEY, paper=PAPER)
data = StockHistoricalDataClient(API_KEY, SECRET_KEY)
app = Flask(__name__)
tz_et = ZoneInfo("America/New_York")

# ──────────────────────────────────────────────
# STATE
# ──────────────────────────────────────────────
pending_entries = {}
open_trades = {}
loss_counts = {}

def today_key():
    return datetime.now(tz=tz_et).strftime("%Y%m%d")

def reset_daily_state_if_needed():
    key = today_key()
    if key not in loss_counts:
        loss_counts.clear()
        loss_counts[key] = {}

def get_live_quote(symbol):
    try:
        q = data.get_stock_latest_quote(StockLatestQuoteRequest(symbol_or_symbols=symbol))
        q = q[symbol]
        bid = float(q.bid_price) if q.bid_price else None
        ask = float(q.ask_price) if q.ask_price else None
        if bid is None and ask is not None:
            bid = ask
        if ask is None and bid is not None:
            ask = bid
        return bid, ask
    except Exception as e:
        print(f"⚠️  Quote fetch failed for {symbol}: {e}", flush=True)
        return None, None

def price_buffer(p):
    return 0.03 if (p is not None and p >= 1.0) else 0.003

def round_lim(p):
    return round(p, 4)

def next_bar_start(now_et):
    return (now_et.replace(second=0, microsecond=0) + timedelta(minutes=1))

def inc_loss(symbol):
    key = today_key()
    d = loss_counts.setdefault(key, {})
    d[symbol] = d.get(symbol, 0) + 1

def losses_for(symbol):
    key = today_key()
    return loss_counts.get(key, {}).get(symbol, 0)

# ──────────────────────────────────────────────
# ENTRY WORKER
# ──────────────────────────────────────────────
def entry_worker():
    while True:
        try:
            reset_daily_state_if_needed()
            now = datetime.now(tz=tz_et)

            for sym, st in list(pending_entries.items()):
                if st["placed"]:
                    continue
                if now >= st["not_before"]:
                    bid, ask = get_live_quote(sym)
                    ref = ask if ask else bid
                    if ref is None:
                        continue
                    limit_price = round_lim(ref + price_buffer(ref))
                    try:
                        trading.submit_order(
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
                        open_trades[sym] = {"entry": limit_price, "qty": st["qty"]}
                        print(f"✅ BUY placed {sym} x{st['qty']} @ {limit_price} (source={st['source']})", flush=True)
                    except Exception as e:
                        print(f"❌ BUY submit failed {sym}: {e}", flush=True)

            # 19:59 ET forced closure
            if now.hour == 19 and now.minute == 59:
                force_close_all()
                time.sleep(5)
        except Exception as e:
            print(f"⚠️ entry_worker error: {e}", flush=True)
        time.sleep(1)

def chase_sell(symbol, qty, target_close):
    bid, ask = get_live_quote(symbol)
    ref = bid if bid else ask
    if ref is None:
        return
    lim = min(ref, target_close)
    lim = round_lim(max(0.0001, lim - (0.001 if ref < 1 else 0.01)))
    try:
        trading.submit_order(
            order_data=LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=lim,
                extended_hours=True
            )
        )
        print(f"✅ SELL placed {symbol} x{qty} @ {lim}", flush=True)
    except Exception as e:
        print(f"❌ SELL failed {symbol}: {e}", flush=True)

def force_close_all():
    try:
        positions = trading.get_all_positions()
        if not positions:
            return
        print("🧹 19:59 ET sweep: closing all open positions...", flush=True)
        for p in positions:
            sym = p.symbol
            qty = int(float(p.qty))
            bid, ask = get_live_quote(sym)
            ref = bid if bid else ask
            if ref is None:
                continue
            lim = round_lim(max(0.0001, ref - (0.001 if ref < 1 else 0.01)))
            try:
                trading.submit_order(
                    order_data=LimitOrderRequest(
                        symbol=sym,
                        qty=qty,
                        side=OrderSide.SELL,
                        time_in_force=TimeInForce.DAY,
                        limit_price=lim,
                        extended_hours=True
                    )
                )
                print(f"✅ EOD SELL placed {sym} x{qty} @ {lim}", flush=True)
            except Exception as e:
                print(f"❌ EOD SELL failed {sym}: {e}", flush=True)
    except Exception as e:
        print(f"⚠️ force_close_all error: {e}", flush=True)

threading.Thread(target=entry_worker, daemon=True).start()

# ──────────────────────────────────────────────
# ROUTES
# ──────────────────────────────────────────────
@app.get("/health")
def health():
    return jsonify({"ok": True, "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")})

@app.post("/tv")
def tv():
    """TradingView webhook endpoint."""
    try:
        raw_body = request.data.decode("utf-8", errors="ignore")
        print(f"🔍 Raw webhook body: {raw_body}", flush=True)
        payload = json.loads(raw_body)
    except Exception as e:
        print(f"⛔ Failed to parse JSON: {e}", flush=True)
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    if not payload or payload.get("secret") != WEBHOOK_SECRET:
        print(f"⛔ Unauthorized webhook attempt: {payload}", flush=True)
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    action = str(payload.get("action", "")).upper().strip()
    symbol = str(payload.get("ticker", "")).upper().strip()

    # Handle blank ticker dynamically
    if not symbol:
        possible_tv_field = payload.get("message") or payload.get("tv_ticker") or ""
        symbol = str(possible_tv_field).upper().strip()
        if not symbol:
            symbol = "UNKNOWN"
            print(f"⚠️ Blank ticker received — using placeholder. Payload: {payload}", flush=True)
        else:
            print(f"⚙️ Auto-filled ticker from message: {symbol}", flush=True)

    qty = int(payload.get("quantity", 0))
    source = str(payload.get("source", "UNKNOWN"))
    target_close = float(payload.get("signal_close", 0.0))

    print(f"✅ Parsed payload: action={action} symbol={symbol} qty={qty} source={source}", flush=True)
    reset_daily_state_if_needed()

    # BUY logic
    if action == "BUY":
        if losses_for(symbol) >= 2:
            print(f"🚫 Blocked BUY for {symbol}: 2-loss daily limit hit.", flush=True)
            return jsonify({"ok": False, "blocked": True}), 200

        nb = next_bar_start(datetime.now(tz=tz_et))
        pending_entries[symbol] = {
            "qty": qty,
            "source": source,
            "not_before": nb,
            "placed": False,
            "entry_limit": None,
        }
        print(f"🕒 Pending BUY for {symbol} x{qty} ({source}) at next bar {nb.strftime('%H:%M:%S ET')}", flush=True)
        return jsonify({"ok": True, "pending": True, "symbol": symbol})

    # SELL logic
    elif action == "SELL":
        if symbol in pending_entries:
            del pending_entries[symbol]
            print(f"ℹ️  Removed pending BUY for {symbol} due to SELL signal.", flush=True)

        try:
            pos = trading.get_open_position(symbol)
            qty = int(float(pos.qty))
        except Exception:
            print(f"ℹ️  No open position for {symbol}.", flush=True)
            return jsonify({"ok": True, "sold": False}), 200

        entry_info = open_trades.get(symbol)
        if entry_info and target_close < entry_info["entry"]:
            inc_loss(symbol)

        print(f"📩 SELL for {symbol} x{qty} target_close={target_close}", flush=True)
        chase_sell(symbol, qty, target_close)
        if symbol in open_trades:
            del open_trades[symbol]
        return jsonify({"ok": True, "sold": True})

    else:
        return jsonify({"ok": False, "error": "unknown_action"}), 400

# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")))








































































