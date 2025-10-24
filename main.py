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
def round_tick(p): return round(float(p)+1e-9, 2)
def log(msg): print(f"{ts()} {msg}", flush=True)

def is_rth(dt=None):
    dt = dt or datetime.now(NY)
    return dt.replace(hour=9, minute=30) <= dt <= dt.replace(hour=16, minute=0)

def safe_qty(sym):
    try: return float(api.get_position(sym).qty)
    except: return 0.0

def latest_bid_ask(sym):
    try:
        q = api.get_latest_quote(sym)
        return (q.bidprice, q.askprice)
    except Exception as e:
        log(f"⚠️ quote error {sym}: {e}")
        return (None, None)

def cancel_all(sym):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == sym:
                api.cancel_order(o.id)
                log(f"🧹 Cancelled {sym}")
    except: pass

def submit_limit(side, sym, qty, px, extended):
    try:
        return api.submit_order(symbol=sym, side=side, qty=qty, type="limit",
                                time_in_force="day", limit_price=round_tick(px),
                                extended_hours=extended)
    except Exception as e:
        log(f"❌ {side.upper()} limit error {sym}: {e}")
        return None

def submit_market(side, sym, qty):
    try:
        return api.submit_order(symbol=sym, side=side, qty=qty, type="market", time_in_force="day")
    except Exception as e:
        log(f"❌ {side.upper()} market error {sym}: {e}")
        return None

#──────────────────────────────────────────────
# JOURNAL + STATS
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
    data = load_log()
    buys = [d for d in data if d["action"] == "BUY" and d["symbol"] == sym]
    if not buys: return
    entry = buys[-1]
    qty = entry.get("quantity", 0)
    entry_price = entry.get("entry_price", 0)
    pnl_d = (exit_price - entry_price) * qty
    pnl_p = ((exit_price / entry_price) - 1) * 100 if entry_price else 0

    trades = [t for t in data if t["action"] in ["EXIT", "STOP", "EXIT_FORCED"]]
    total_trades = len(trades) + 1
    total_profit = sum(t.get("PnL$", 0) for t in trades) + pnl_d
    wins = sum(1 for t in trades if t.get("PnL$", 0) > 0) + (1 if pnl_d > 0 else 0)
    win_rate = round((wins / total_trades) * 100, 2)
    avg_pnl = round(total_profit / total_trades / (entry_price * qty / 100) if total_trades > 0 else 0, 2)

    record = {
        "time": ts(),
        "symbol": sym,
        "action": "EXIT",
        "entry_price": entry_price,
        "exit_price": exit_price,
        "quantity": qty,
        "PnL$": round(pnl_d, 2),
        "PnL%": round(pnl_p, 2),
        "total_profit": round(total_profit, 2),
        "total_trades": total_trades,
        "wins": wins,
        "win_rate": win_rate,
        "avg_pnl%": avg_pnl
    }
    data.append(record)
    save_log(data)
    log(f"💰 {sym} closed | PnL%={pnl_p:.2f} | total={total_profit:.2f} | WR={win_rate}%")

#──────────────────────────────────────────────
# EXIT MANAGEMENT
#──────────────────────────────────────────────
def managed_exit(sym, qty_hint, vwap, mama):
    try:
        qty = safe_qty(sym) or qty_hint
        if qty <= 0: return
        target = round_tick(min([p for p in [vwap, mama] if p > 0], key=lambda x: x))
        end = datetime.now(NY) + timedelta(minutes=10)
        log(f"🟣 Exit target {sym}@{target}")

        while datetime.now(NY) < end:
            if safe_qty(sym) <= 0:
                update_pnl(sym, target)
                return
            cancel_all(sym)
            bid, ask = latest_bid_ask(sym)
            px = bid or target
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
# STOP MONITOR  (2% LIMIT STOP, SYNTHETIC)
#──────────────────────────────────────────────
def stop_monitor(sym, entry_price, stop_pct=0.02):
    try:
        threshold = round_tick(entry_price * (1 - stop_pct))
        log(f"🛑 Monitoring {sym} 2% stop @ {threshold}")
        while True:
            qty = safe_qty(sym)
            if qty <= 0:
                return

            bid, ask = latest_bid_ask(sym)
            prices = [p for p in [bid, ask] if p and p > 0]
            last = min(prices) if prices else entry_price

            # Refresh the synthetic stop-limit order every 10s
            cancel_all(sym)
            stop_px = round_tick(threshold)
            submit_limit("sell", sym, qty, stop_px, extended=True)

            if last <= threshold:
                log(f"🛑 Stop triggered {sym}@{last}")
                update_pnl(sym, last)
                return

            time.sleep(10)
    except Exception as e:
        log(f"❌ stop_monitor {sym}: {e}\n{traceback.format_exc()}")

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
        vwap = float(data.get("vwap", 0))
        mama = float(data.get("mama", 0))
        rth = is_rth()

        if act == "BUY":
            entry = round_tick(close_price * 1.005)
            cancel_all(sym)
            o = submit_limit("buy", sym, qty, entry, extended=True)
            if o:
                time.sleep(2)
                try:
                    pos = api.get_position(sym)
                    entry_px = float(pos.avg_entry_price)
                    threading.Thread(target=stop_monitor, args=(sym, entry_px, 0.02), daemon=True).start()
                    log(f"✅ BUY filled {sym}@{entry_px}")
                    write_log({"time": ts(), "symbol": sym, "action": "BUY", "entry_price": entry_px, "quantity": qty})
                except:
                    log("🕒 Waiting for fill...")
            return jsonify(status="buy_ok"), 200

        if act == "EXIT":
            log(f"🔔 EXIT {sym}")
            threading.Thread(target=managed_exit, args=(sym, qty, vwap, mama), daemon=True).start()
            return jsonify(status="exit_started"), 200

        return jsonify(status="ignored"), 200
    except Exception as e:
        log(f"❌ {e}\n{traceback.format_exc()}")
        return jsonify(err="server"), 500

#──────────────────────────────────────────────
# RUN
#──────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


























