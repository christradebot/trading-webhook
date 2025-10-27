# main.py v2.3 — © Athena + Chris 2025
# Unified handler for ITG Scalper + Hammer logic
# Limit-only bot with ATR-based stops, PnL summary, and daily reset

from flask import Flask, request, jsonify
import os, json, time, traceback
from datetime import datetime, timedelta
import pytz
import numpy as np
from alpaca_trade_api.rest import REST

#────────────────────────────────────────────
# Config & Environment
#────────────────────────────────────────────
app = Flask(__name__)
ALPACA_KEY_ID     = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET", "mysecret")
VERBOSE           = bool(int(os.getenv("VERBOSE", "1")))  # 1=on, 0=off
api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version='v2')
NY = pytz.timezone("America/New_York")

#────────────────────────────────────────────
# State Tracking
#────────────────────────────────────────────
loss_count = {}
added_once = {}
trade_stats = {}

def daily_reset():
    now = datetime.now(NY)
    if now.hour == 0 and now.minute < 5:
        loss_count.clear()
        added_once.clear()
        trade_stats.clear()
        log("🔄 Daily reset complete (adds/losses cleared)")

#────────────────────────────────────────────
# Helpers
#────────────────────────────────────────────
def log(msg, force=False):
    if VERBOSE or force:
        print(f"[{datetime.now(NY).strftime('%H:%M:%S')}] {msg}", flush=True)

def round_tick(price): return round(price, 4)

def safe_qty(sym):
    try: pos = api.get_position(sym); return float(pos.qty)
    except Exception: return 0

def latest_bid_ask(sym):
    try:
        q = api.get_latest_quote(sym)
        return q.bp, q.ap
    except Exception:
        return (0, 0)

def cancel_all(sym):
    for o in api.list_orders(status="open"):
        if o.symbol == sym:
            api.cancel_order(o.id)

def current_atr(sym, period=14):
    bars = api.get_bars(sym, "1Min", limit=period*2).df
    if len(bars) < period: return 0
    highs, lows, closes = bars['high'], bars['low'], bars['close']
    trs = np.maximum(highs[1:] - lows[1:], np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1]))
    return float(np.mean(trs[-period:]))

def update_pnl(sym):
    try:
        pos = api.get_position(sym)
        unreal = float(pos.unrealized_pl)
        unreal_pct = float(pos.unrealized_plpc) * 100
        log(f"📈 {sym} Unrealized: ${unreal:.2f} ({unreal_pct:.2f}%)")
    except Exception:
        log(f"📉 {sym} closed or no active position.")

def record_trade(sym, realized=0, add=False, loss=False):
    st = trade_stats.setdefault(sym, {"realized":0,"adds":0,"losses":0,"trades":0})
    st["realized"] += realized
    st["trades"] += 1
    if add: st["adds"] += 1
    if loss: st["losses"] += 1

def summary(sym):
    s = trade_stats.get(sym, {"realized":0,"adds":0,"losses":0,"trades":0})
    log(f"📊 SUMMARY — {sym}\n"
        f"    Realized PnL: ${s['realized']:.2f}\n"
        f"    Trades: {s['trades']}\n"
        f"    Adds Used: {s['adds']}\n"
        f"    Losses Today: {s['losses']}", force=True)

#────────────────────────────────────────────
# Core Trading Logic
#────────────────────────────────────────────
def managed_exit(sym, qty_hint):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0: return
        bid, ask = latest_bid_ask(sym)
        trade = api.get_latest_trade(sym)
        target = round_tick(bid or ask or (trade.price if trade and trade.price > 0 else 0))
        if target <= 0:
            log(f"⚠️ No valid price for exit {sym}, skipping.")
            return
        log(f"🟣 Exit target {sym}@{target}")
        cancel_all(sym)
        api.submit_order(side="sell", type="limit", time_in_force="day",
                         symbol=sym, qty=qty, limit_price=target, extended_hours=True)
        time.sleep(10)
        try:
            pos = api.get_position(sym)
            unreal = float(pos.unrealized_pl)
            log(f"📈 {sym} Unrealized after exit try: ${unreal:.2f}")
        except Exception:
            trade_hist = api.get_activities("FILL", until=datetime.now(NY))
            realized = 0
            for t in trade_hist:
                if getattr(t, "symbol", "") == sym:
                    realized += float(getattr(t, "net_amount", 0))
            record_trade(sym, realized=realized)
            summary(sym)
    except Exception as e:
        log(f"❌ managed_exit {sym}: {e}\n{traceback.format_exc()}")

def handle_buy(sym, entry_price, candle_low, qty):
    try:
        now = datetime.now(NY)
        atr = current_atr(sym)
        atr_mult = 3 if now.hour == 9 and 30 <= now.minute <= 45 else 1
        stop = candle_low - (atr * atr_mult)
        range_pc = (entry_price - candle_low) / entry_price * 100
        if range_pc > 10:
            log(f"⚠️ Skipping {sym}: range {range_pc:.1f}% > 10%")
            return
        cancel_all(sym)
        limit_price = round_tick(entry_price)
        log(f"🟢 BUY {sym} @ {limit_price} | Stop {round_tick(stop)} | ATR×{atr_mult}")
        api.submit_order(side="buy", type="limit", time_in_force="day",
                         symbol=sym, qty=qty, limit_price=limit_price, extended_hours=True)
        update_pnl(sym)
        record_trade(sym)
    except Exception as e:
        log(f"❌ handle_buy {sym}: {e}\n{traceback.format_exc()}")

def handle_add(sym, entry_price, candle_low, qty):
    try:
        if added_once.get(sym):
            log(f"⚠️ {sym} already added once; skipping additional add.")
            return
        unreal = 0
        try:
            pos = api.get_position(sym)
            unreal = float(pos.unrealized_plpc)
        except Exception: pass
        if unreal <= 0:
            log(f"⚠️ {sym} not profitable; skipping ADD.")
            return
        added_once[sym] = True
        limit_price = round_tick(entry_price)
        log(f"🟨 ADD {sym} @ {limit_price}")
        api.submit_order(side="buy", type="limit", time_in_force="day",
                         symbol=sym, qty=qty, limit_price=limit_price, extended_hours=True)
        update_pnl(sym)
        record_trade(sym, add=True)
    except Exception as e:
        log(f"❌ handle_add {sym}: {e}\n{traceback.format_exc()}")

#────────────────────────────────────────────
# Webhook Handler
#────────────────────────────────────────────
@app.post("/tv")
def tv():
    daily_reset()
    try:
        data = request.get_json(silent=True) or {}
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify(error="Invalid secret"), 403

        sym = data.get("ticker")
        action = data.get("action", "").upper()
        qty = int(data.get("quantity", 100))
        entry_price = float(data.get("entry_price", 0))
        candle_low = float(data.get("candle_low", entry_price * 0.98))
        if not sym or entry_price <= 0:
            return jsonify(error="Invalid payload"), 400

        log(f"🚀 Alert {action} {sym} @ {entry_price}")

        if action == "BUY": handle_buy(sym, entry_price, candle_low, qty)
        elif action == "EXIT": managed_exit(sym, qty)
        elif action == "ADD": handle_add(sym, entry_price, candle_low, qty)
        else: log(f"⚠️ Unknown action {action}")

        return jsonify(ok=True)
    except Exception as e:
        log(f"❌ tv handler: {e}\n{traceback.format_exc()}")
        return jsonify(error=str(e)), 500

@app.get("/ping")
def ping(): return jsonify(ok=True, time=datetime.now(NY).isoformat())

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)))


































