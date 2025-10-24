from flask import Flask, request, jsonify
import os, json, time, threading, traceback
from datetime import datetime
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
# UTILITIES
#──────────────────────────────────────────────
def ts(): return datetime.now(NY).strftime("[%H:%M:%S]")
def round_tick(p): return round(float(p) + 1e-9, 2)
def log(msg): print(f"{ts()} {msg}", flush=True)

def safe_qty(sym):
    try:
        return float(api.get_position(sym).qty)
    except:
        return 0.0

def cancel_all(sym):
    try:
        for o in api.list_orders(status="open"):
            if o.symbol == sym:
                api.cancel_order(o.id)
                log(f"🧹 Cancelled open order {sym} ({o.id})")
    except Exception as e:
        log(f"⚠️ cancel_all error: {e}")

def submit_limit(side, sym, qty, px, extended):
    try:
        px = round_tick(px)
        order = api.submit_order(
            symbol=sym, side=side, qty=qty, type="limit",
            time_in_force="day", limit_price=px,
            extended_hours=extended
        )
        log(f"📩 {side.upper()} LIMIT {sym}@{px} x{qty}")
        return order
    except Exception as e:
        log(f"❌ {side.upper()} limit error {sym}: {e}")
        return None

#──────────────────────────────────────────────
# JOURNAL / PNL
#──────────────────────────────────────────────
def load_log():
    if not os.path.exists(TRADE_LOG_PATH):
        with open(TRADE_LOG_PATH, "w") as f:
            json.dump([], f)
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

def update_pnl(sym, entry_price, exit_price, qty):
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
    write_log(record)
    log(f"💰 EXIT {sym} | PnL={pnl_p:.2f}% | ${pnl_d:.2f}")

#──────────────────────────────────────────────
# HARD STOP-LOSS (LIMIT-ONLY, FLOAT-SAFE)
#──────────────────────────────────────────────
def stop_monitor(sym, entry_price, stop_pct=0.02):
    """Unified limit-only stop-loss, valid pre/post-market."""
    try:
        threshold = round_tick(entry_price * (1 - stop_pct))
        limit_px  = round_tick(threshold * 0.999)  # small buffer for fill
        log(f"🛡️ Stop monitor active for {sym} | −{stop_pct*100:.1f}% stop @ {threshold}")

        while True:
            qty = safe_qty(sym)
            if qty <= 0:
                return

            try:
                q = api.get_latest_quote(sym)
                bid = float(q.bidprice or 0)
                ask = float(q.askprice or 0)
                trade_px = float(api.get_latest_trade(sym).price or 0)

                # select best available nonzero price
                prices = [p for p in [bid, ask, trade_px] if p > 0]
                last = max(prices) if prices else entry_price
            except Exception as e:
                log(f"⚠️ quote error {sym}: {e}")
                last = entry_price

            if last <= threshold:
                cancel_all(sym)
                log(f"🛑 LIMIT STOP TRIGGERED {sym}@{last} (limit {limit_px})")
                submit_limit("sell", sym, qty, limit_px, extended=True)
                update_pnl(sym, entry_price, last, qty)
                return

            time.sleep(3)
    except Exception as e:
        log(f"❌ stop_monitor error {sym}: {e}\n{traceback.format_exc()}")

#──────────────────────────────────────────────
# MAIN WEBHOOK HANDLER
#──────────────────────────────────────────────
@app.route("/tv", methods=["POST"])
def tv():
    try:
        data = request.get_json(force=True) or {}
        if data.get("secret") != WEBHOOK_SECRET:
            return jsonify(error="bad secret"), 403

        action = data.get("action", "").upper()
        sym = data.get("ticker", "").upper()
        qty = float(data.get("quantity", 0))
        extended = True  # always allow extended hours

        #──────────── Ensure no duplicate trades ────────────
        existing_positions = [p.symbol for p in api.list_positions()]
        if sym in existing_positions:
            log(f"⚠️ {sym} still open — closing before new trade")
            pos = api.get_position(sym)
            qty_open = float(pos.qty)
            last = float(pos.current_price)
            entry_px = float(pos.avg_entry_price)
            if qty_open > 0:
                submit_limit("sell", sym, qty_open, last * 0.999, extended=True)
                update_pnl(sym, entry_px, last, qty_open)
                cancel_all(sym)
                time.sleep(1)

        #──────────── BUY ────────────
        if action == "BUY":
            entry_price = float(data.get("entry_price", 0))
            if not entry_price or qty <= 0:
                return jsonify(error="missing params"), 400

            cancel_all(sym)
            submit_limit("buy", sym, qty, entry_price, extended)
            write_log({
                "time": ts(),
                "symbol": sym,
                "action": "BUY",
                "entry_price": entry_price,
                "quantity": qty
            })
            log(f"✅ BUY triggered {sym}@{entry_price}")

            threading.Thread(target=stop_monitor, args=(sym, entry_price, 0.02), daemon=True).start()
            return jsonify(status="buy_sent"), 200

        #──────────── EXIT ────────────
        if action == "EXIT":
            exit_price = float(data.get("exit_price", 0))
            if not exit_price or qty <= 0:
                return jsonify(error="missing params"), 400

            cancel_all(sym)
            submit_limit("sell", sym, qty, exit_price, extended)

            # find last BUY
            data_log = load_log()
            entries = [t for t in data_log if t["symbol"] == sym and t["action"] == "BUY"]
            entry_px = entries[-1]["entry_price"] if entries else exit_price
            update_pnl(sym, entry_px, exit_price, qty)
            log(f"🔔 EXIT triggered {sym}@{exit_price}")
            return jsonify(status="exit_sent"), 200

        return jsonify(status="ignored"), 200

    except Exception as e:
        log(f"❌ Webhook error: {e}\n{traceback.format_exc()}")
        return jsonify(error="server_error"), 500

#──────────────────────────────────────────────
# MAIN ENTRYPOINT
#──────────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

























