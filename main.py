# === main.py ===
# Momentum Scalper Bot — Signal Candle Low Stop + Auto ATR Buffer (9:30–9:45 ET)
# Includes ATR + Stop Distance Logging + Trade Summary + 10% Range Filter
# © Athena + Chris 2025

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import os, json, time, traceback, threading

# ────────────────────────────────
# ENV + API
# ────────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
NY = ZoneInfo("America/New_York")

app = Flask(__name__)

LOSS_CAP = 2
loss_count = {}
open_positions = {}  # Track entries for summary logs


# ────────────────────────────────
# HELPERS
# ────────────────────────────────
def ts():
    return datetime.now(NY).strftime("[%H:%M:%S]")

def log(msg):
    print(f"{ts()} {msg}", flush=True)

def round_tick(px):
    try:
        return round(float(px) + 1e-9, 2)
    except:
        return 0.0

def safe_qty(sym):
    try:
        return float(api.get_position(sym).qty)
    except:
        return 0.0

def latest_bid_ask(sym):
    try:
        q = api.get_latest_quote(sym)
        return float(q.bidprice or 0.0), float(q.askprice or 0.0)
    except Exception:
        return 0.0, 0.0

def cancel_all(sym):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == sym:
                api.cancel_order(o.id)
        log(f"🧹 Cancelled open orders for {sym}")
    except Exception as e:
        log(f"⚠️ cancel_all {sym}: {e}")

def submit_limit(side, sym, qty, px):
    try:
        if px <= 0 or qty <= 0:
            log(f"⚠️ Invalid order {side} {sym} qty={qty} px={px}")
            return None
        o = api.submit_order(
            symbol=sym,
            side=side,
            qty=qty,
            type="limit",
            time_in_force="day",
            limit_price=round_tick(px),
            extended_hours=True
        )
        log(f"📤 {side.upper()} {qty} {sym} @ {round_tick(px)} (extended=True, TIF=day)")
        return o
    except Exception as e:
        log(f"❌ {side.upper()} limit error {sym}: {e}")
        return None

def in_open_window(dt):
    # Returns True between 9:30–9:45 AM ET
    return dt.tzinfo and dt.hour == 9 and 30 <= dt.minute <= 45


# ────────────────────────────────
# AGGRESSIVE EXIT LOOP
# ────────────────────────────────
def force_exit_until_flat(sym, ref_price):
    """Exit immediately using aggressive limit orders until flat."""
    try:
        entry_px = open_positions.get(sym, {}).get("entry", None)
        atr = open_positions.get(sym, {}).get("atr", 0)
        atr_mult = open_positions.get(sym, {}).get("atr_mult", 1)

        end = datetime.now(NY) + timedelta(seconds=90)
        while datetime.now(NY) < end:
            qty = safe_qty(sym)
            if qty <= 0:
                exit_px = ref_price
                if entry_px:
                    pnl = ((exit_px - entry_px) / entry_px) * 100
                    log(f"📊 Closed {sym} | Entry {round_tick(entry_px)} | Exit {round_tick(exit_px)} | P/L {round(pnl,2)}% | ATR {round(atr,2)} × {atr_mult}")
                log(f"✅ Flat {sym} after stop or exit.")
                open_positions.pop(sym, None)
                return
            cancel_all(sym)
            bid, ask = latest_bid_ask(sym)
            px = round_tick((bid if bid > 0 else ref_price or ask or 0.01) - 0.05)
            submit_limit("sell", sym, qty, px)
            time.sleep(2)

        qty = safe_qty(sym)
        if qty > 0:
            bid, ask = latest_bid_ask(sym)
            deep_px = round_tick((bid if bid > 0 else ref_price or ask or 0.01) - 0.10)
            log(f"⚠️ Final exit {sym}@{deep_px}")
            cancel_all(sym)
            submit_limit("sell", sym, qty, deep_px)
            time.sleep(2)
    except Exception as e:
        log(f"❌ force_exit_until_flat {sym}: {e}\n{traceback.format_exc()}")


# ────────────────────────────────
# STOP MONITOR
# ────────────────────────────────
def monitor_stop(sym, entry_px, signal_low, atr):
    try:
        now = datetime.now(NY)
        atr_mult = 3.0 if in_open_window(now) else 1.0
        stop = signal_low - (atr * atr_mult if atr > 0 else 0)
        stop = round_tick(max(0.01, stop))

        # Calculate stop distance
        distance = abs(entry_px - stop)
        pct = (distance / entry_px) * 100 if entry_px > 0 else 0
        log(f"🛡 {sym} STOP SETUP → entry={round_tick(entry_px)} | low={signal_low} | ATR={round_tick(atr)} | mult={atr_mult} | stop={stop} | distance={round(pct,2)}%")

        open_positions[sym] = {"entry": entry_px, "atr": atr, "atr_mult": atr_mult}

        while True:
            qty = safe_qty(sym)
            if qty <= 0:
                return
            bid, ask = latest_bid_ask(sym)
            last = bid or ask or entry_px
            if last <= stop:
                log(f"🛑 STOP TRIGGERED {sym} last={round_tick(last)} <= stop={stop}")
                force_exit_until_flat(sym, ref_price=last)
                loss_count[sym] = loss_count.get(sym, 0) + 1
                log(f"📉 Loss #{loss_count[sym]} for {sym}")
                return
            time.sleep(1.5)
    except Exception as e:
        log(f"❌ monitor_stop {sym}: {e}\n{traceback.format_exc()}")


# ────────────────────────────────
# ENTRY LOGIC
# ────────────────────────────────
def try_enter(sym, qty, entry_price, signal_low, atr, signal_high):
    if loss_count.get(sym, 0) >= LOSS_CAP:
        log(f"🚫 {sym}: Max losses reached ({LOSS_CAP}) → skipping.")
        return jsonify(status="blocked"), 200

    # Skip if candle range > 10%
    if signal_high > 0 and signal_low > 0:
        candle_range = ((signal_high - signal_low) / entry_price) * 100
        if candle_range > 10:
            log(f"🚫 {sym}: Candle range {round(candle_range,2)}% > 10% → skipped.")
            return jsonify(status="skipped_range"), 200

    if safe_qty(sym) > 0:
        log(f"ℹ️ {sym}: Already in position.")
        return jsonify(status="already_in"), 200

    px = round_tick(entry_price)
    order = submit_limit("buy", sym, qty, px)
    if not order:
        return jsonify(status="rejected"), 200

    time.sleep(2)
    try:
        pos = api.get_position(sym)
        avg_entry = float(pos.avg_entry_price)
        log(f"✅ Filled {sym}@{avg_entry}")
        threading.Thread(target=monitor_stop, args=(sym, avg_entry, signal_low, atr), daemon=True).start()
    except Exception:
        log(f"🕒 Pending fill for {sym} → monitoring anyway")
        threading.Thread(target=monitor_stop, args=(sym, px, signal_low, atr), daemon=True).start()

    return jsonify(status="entry_ok"), 200


# ────────────────────────────────
# EXIT SIGNAL HANDLER
# ────────────────────────────────
def exit_on_signal(sym):
    qty = safe_qty(sym)
    if qty <= 0:
        return jsonify(status="no_position"), 200
    bid, ask = latest_bid_ask(sym)
    px = round_tick((bid or ask or 0.01) - 0.02)
    cancel_all(sym)
    submit_limit("sell", sym, qty, px)
    force_exit_until_flat(sym, ref_price=px)
    return jsonify(status="exit_ok"), 200


# ────────────────────────────────
# WEBHOOK
# ────────────────────────────────
@app.post("/tv")
def tv():
    try:
        data = request.get_json(force=True) or {}
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify(err="bad secret"), 403

        act = str(data.get("action", "")).upper()
        sym = str(data.get("ticker", "")).upper()
        qty = float(data.get("quantity", 0) or 0)
        entry_price = float(data.get("entry_price", 0) or 0)
        signal_low = float(data.get("signal_low", 0) or 0)
        signal_high = float(data.get("signal_high", 0) or 0)
        atr = float(data.get("atr", 0) or 0)

        if not sym:
            return jsonify(err="missing symbol"), 400

        if act == "BUY":
            if entry_price <= 0 or signal_low <= 0 or qty <= 0:
                log(f"⚠️ Bad BUY payload for {sym}")
                return jsonify(status="bad_payload"), 400
            log(f"🚀 BUY {sym} qty={qty} entry={entry_price} stop@low={signal_low}")
            return try_enter(sym, qty, entry_price, signal_low, atr, signal_high)

        if act == "EXIT":
            log(f"🔔 EXIT {sym} received")
            return exit_on_signal(sym)

        return jsonify(status="ignored"), 200

    except Exception as e:
        log(f"❌ webhook error: {e}\n{traceback.format_exc()}")
        return jsonify(err="server_error"), 500


# ────────────────────────────────
@app.get("/ping")
def ping():
    return jsonify(ok=True, service="Athena Scalper Bot", base=ALPACA_BASE_URL)


# ────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))




























