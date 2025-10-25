# === main.py ===
# © Chris / Athena 2025
# Stable release with target_price + 2% stop-loss (limit-only)
from flask import Flask, request, jsonify
import os, json, time, threading, traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from alpaca_trade_api.rest import REST

app = Flask(__name__)

#──────────────────────────────────────────────
# ENVIRONMENT
#──────────────────────────────────────────────
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "chrisbot1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")
NY = ZoneInfo("America/New_York")
TRADE_LOG_PATH = "/app/trade_log.json"

#──────────────────────────────────────────────
# UTILITY
#──────────────────────────────────────────────
def ts(): return datetime.now(NY).strftime("[%H:%M:%S]")
def round_tick(p): return round(float(p) + 1e-9, 2)
def log(msg): print(f"{ts()} {msg}", flush=True)

def safe_qty(sym):
    try: return float(api.get_position(sym).qty)
    except: return 0.0

def latest_bid_ask(sym):
    try:
        q = api.get_latest_quote(sym)
        return (q.bidprice, q.askprice)
    except:
        return (0.0, 0.0)

def cancel_all(sym):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == sym:
                api.cancel_order(o.id)
                log(f"🧹 Cancelled {sym}")
    except: pass

def submit_limit(side, sym, qty, px, extended):
    try:
        if px <= 0:
            log(f"⚠️ Invalid {side.upper()} price {px} for {sym}, skipping.")
            return None
        return api.submit_order(
            symbol=sym,
            side=side,
            qty=qty,
            type="limit",
            time_in_force="day",
            limit_price=round_tick(px),
            extended_hours=extended
        )
    except Exception as e:
        log(f"❌ {side.upper()} limit error {sym}: {e}")
        return None

#──────────────────────────────────────────────
# LOGGING
#──────────────────────────────────────────────
def load_log():
    if not os.path.exists(TRADE_LOG_PATH):
        with open(TRADE_LOG_PATH, "w") as f: json.dump([], f)
    with open(TRADE_LOG_PATH, "r") as f:
        return json.load(f)

def save_log(data):
    with open(TRADE_LOG_PATH, "w") as f:
        json.dump(data, f, indent=2)

def write_log(entry):
    try:
        data = load_log()
        data.append(entry)
        save_log(data)
    except Exception as e:
        log(f"⚠️ log write failed: {e}")

def update_pnl(sym, exit_price):
    try:
        data = load_log()
        buys = [d for d in data if d["action"] == "BUY" and d["symbol"] == sym]
        if not buys: return
        entry = buys[-1]
        qty = entry.get("quantity", 0)
        entry_price = entry.get("entry_price", 0)
        pnl_d = (exit_price - entry_price) * qty
        pnl_p = ((exit_price / entry_price) - 1) * 100 if entry_price else 0
        record = {
            "time": ts(),
            "symbol": sym,
            "action": "EXIT",
            "entry_price": entry_price,
            "exit_price": exit_price,
            "quantity": qty,
            "PnL$": round(pnl_d, 2),
            "PnL%": round(pnl_p, 2)
        }
        data.append(record)
        save_log(data)
        log(f"💰 {sym} closed | PnL%={pnl_p:.2f}")
    except Exception as e:
        log(f"⚠️ update_pnl failed: {e}")

#──────────────────────────────────────────────
# STOP MONITOR
#──────────────────────────────────────────────
def stop_monitor(sym, entry_price):
    try:
        threshold = round_tick(entry_price * 0.98)  # 2% stop-loss
        while True:
            qty = safe_qty(sym)
            if qty <= 0: return
            bid, ask = latest_bid_ask(sym)
            trade = api.get_latest_trade(sym)
            last = bid or ask or (trade.price if trade and trade.price > 0 else 0)
            if last <= threshold and last > 0:
                log(f"🛑 Stop-loss triggered {sym}@{last} (2%)")
                cancel_all(sym)
                px = round_tick((bid or last) - 0.01)
                submit_limit("sell", sym, qty, px, extended=True)
                update_pnl(sym, last)
                return
            time.sleep(5)
    except Exception as e:
        log(f"❌ stop_monitor {sym}: {e}")

#──────────────────────────────────────────────
# MANAGED EXIT (Updated)
#──────────────────────────────────────────────
def managed_exit(sym, qty_hint):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0:
            return

        # Use current market data for exit
        bid, ask = latest_bid_ask(sym)
        trade = api.get_latest_trade(sym)
        target = round_tick(bid or ask or (trade.price if trade and trade.price > 0 else 0))
        if target <= 0:
            log(f"⚠️ No valid price for exit {sym}, skipping.")
            return

        end = datetime.now(NY) + timedelta(minutes=10)
        log(f"🟣 Exit target {sym}@{target}")

        while datetime.now(NY) < end:
            if safe_qty(sym) <= 0:
                update_pnl(sym, target)
                return
            cancel_all(sym)
            bid, ask = latest_bid_ask(sym)
            px = round_tick(bid or ask or target)
            submit_limit("sell", sym, qty, px, extended=True)
            time.sleep(20)

        if safe_qty(sym) > 0:
            bid, _ = latest_bid_ask(sym)
            px = round_tick((bid or target) - 0.01)
            log(f"⚠️ Expired → LIMIT exit {sym}@{px} XH")
            submit_limit("sell", sym, qty, px, extended=True)
            update_pnl(sym, target)
    except Exception as e:
        log(f"❌ managed_exit {sym}: {e}\n{traceback.format_exc()}")

#──────────────────────────────────────────────
# TARGET MONITOR (Optional)
#──────────────────────────────────────────────
def target_monitor(sym, target_price):
    try:
        if target_price <= 0:
            return
        qty = safe_qty(sym)
        while qty > 0:
            bid, ask = latest_bid_ask(sym)
            trade = api.get_latest_trade(sym)
            last = bid or ask or (trade.price if trade and trade.price > 0 else 0)
            if last >= target_price:
                log(f"🎯 TP hit {sym}@{target_price}")
                cancel_all(sym)
                submit_limit("sell", sym, qty, target_price, extended=True)
                update_pnl(sym, target_price)
                return
            time.sleep(5)
            qty = safe_qty(sym)
    except Exception as e:
        log(f"❌ target_monitor {sym}: {e}")

#──────────────────────────────────────────────
# WEBHOOK
#──────────────────────────────────────────────
@app.route("/tv", methods=["POST"])
def tv():
    try:
        data = request.get_json(force=True) or {}
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify(err="bad secret"), 403

        act = data.get("action", "").upper()
        sym = data.get("ticker", "").upper()
        qty = float(data.get("quantity", 0))
        close_price = float(data.get("close_price", 0))
        target_price = float(data.get("target_price", 0))

        if act == "BUY":
            entry = round_tick(close_price * 1.005)
            cancel_all(sym)
            o = submit_limit("buy", sym, qty, entry, extended=True)
            if o:
                log(f"✅ BUY submitted {sym}@{entry}")
                time.sleep(2)
                try:
                    pos = api.get_position(sym)
                    entry_px = float(pos.avg_entry_price)
                    threading.Thread(target=stop_monitor, args=(sym, entry_px), daemon=True).start()
                    if target_price > 0:
                        threading.Thread(target=target_monitor, args=(sym, target_price), daemon=True).start()
                        log(f"🎯 Target set {sym}@{target_price}")
                    write_log({"time": ts(), "symbol": sym, "action": "BUY", "entry_price": entry_px, "quantity": qty})
                except Exception as e:
                    log(f"🕒 Waiting for fill... {e}")
            return jsonify(status="buy_ok"), 200

        if act == "EXIT":
            log(f"🔔 EXIT {sym}")
            threading.Thread(target=managed_exit, args=(sym, qty), daemon=True).start()
            return jsonify(status="exit_started"), 200

        return jsonify(status="ignored"), 200

    except Exception as e:
        log(f"❌ {e}\n{traceback.format_exc()}")
        return jsonify(err="server"), 500

#──────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))



























