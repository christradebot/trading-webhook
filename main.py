# ============================================================
#  main.py — Chris / Athena Trading Bot
#  ATR + EMA Momentum Strategy (EMA5 Logic)
# ============================================================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST
import os, json, datetime, pytz, time

app = Flask(__name__)

# ------------------------------------------------------------
#  Environment
# ------------------------------------------------------------
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "CRIS-1501")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL, api_version="v2")

# ------------------------------------------------------------
#  Config
# ------------------------------------------------------------
EASTERN = pytz.timezone("US/Eastern")
PRE_MARKET_START   = datetime.time(4, 0)
REGULAR_MARKET_END = datetime.time(16, 0)

EMA_BUFFER = 0.002      # 0.2 % entry buffer
EMA_EXIT_TOL = 0.0      # 0 % tolerance around EMA5 for exit
EMERGENCY_LOSS = 20.0   # % fallback synthetic stop

# ------------------------------------------------------------
#  Utilities
# ------------------------------------------------------------
def now_et():
    return datetime.datetime.now(EASTERN)

def in_pre_market(t):
    return PRE_MARKET_START <= t.time() < datetime.time(9, 30)

def in_regular(t):
    return datetime.time(9, 30) <= t.time() < REGULAR_MARKET_END

def flatten_at_4am():
    t = now_et()
    if t.hour == 4 and t.minute == 0:
        try:
            for pos in api.list_positions():
                api.close_position(pos.symbol)
                print(f"🕓 Daily reset closed {pos.symbol}")
        except Exception as e:
            print(f"❌ Reset error: {e}")

# ------------------------------------------------------------
#  Order Logic
# ------------------------------------------------------------
def submit_buy(symbol, qty, ema5, price):
    t = now_et()
    entry_level = ema5 if ema5 else price
    if in_pre_market(t):
        limit_price = round(entry_level * (1 + EMA_BUFFER), 2)
        print(f"🟢 Pre-market BUY {symbol} limit @ {limit_price}")
        api.submit_order(
            symbol=symbol, qty=qty, side="buy",
            type="limit", time_in_force="day",
            limit_price=limit_price, extended_hours=True
        )
    elif in_regular(t):
        print(f"🟢 Regular-hours BUY {symbol} market")
        api.submit_order(
            symbol=symbol, qty=qty, side="buy",
            type="market", time_in_force="day"
        )
    else:
        print("⚠️ Market closed; BUY skipped.")

def check_exit_by_ema(symbol, ema5):
    """Close position if price falls below EMA5."""
    try:
        pos = api.get_position(symbol)
        if not pos: return
        qty = float(pos.qty)
        avg = float(pos.avg_entry_price)
        last = float(api.get_latest_trade(symbol).price)
        if last < ema5 * (1 - EMA_EXIT_TOL):
            print(f"🔴 Exit {symbol}: price {last} < EMA5 {ema5}")
            api.submit_order(
                symbol=symbol, qty=qty, side="sell",
                type="market", time_in_force="day", extended_hours=True
            )
    except Exception as e:
        print(f"❌ EMA exit check failed for {symbol}: {e}")

def emergency_stop(symbol, entry_price):
    """Synthetic 20 % stop for pre-market."""
    try:
        last = float(api.get_latest_trade(symbol).price)
        loss_pct = (entry_price - last) / entry_price * 100
        if loss_pct >= EMERGENCY_LOSS:
            limit_price = round(last * 0.995, 2)
            print(f"🚨 Emergency stop {symbol} @ {limit_price}")
            api.submit_order(
                symbol=symbol, qty=1, side="sell",
                type="limit", time_in_force="day",
                limit_price=limit_price, extended_hours=True
            )
    except Exception as e:
        print(f"❌ Emergency stop error: {e}")

# ------------------------------------------------------------
#  Webhook
# ------------------------------------------------------------
@app.route("/tv", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return jsonify({"error": "No data"}), 400
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Bad secret"}), 403

    print("\n🚀 TradingView Alert:")
    print(json.dumps(data, indent=2))

    event  = data.get("event", "").upper()
    symbol = data.get("ticker", "")
    qty    = float(data.get("qty", 1))
    price  = float(data.get("price", 0))
    ema5   = float(data.get("ema5", 0))

    try:
        flatten_at_4am()

        if event == "BUY":
            submit_buy(symbol, qty, ema5, price)

        elif event in ["EXIT", "SELL", "STOP"]:
            # manual or indicator exit
            print(f"🛑 EXIT signal for {symbol}")
            api.close_position(symbol)

        elif event == "CHECK_EMA_EXIT":
            # optional scheduled alert to check EMA exit mid-bar
            check_exit_by_ema(symbol, ema5)

        else:
            print(f"⚠️ Unknown event {event}")

    except Exception as e:
        print(f"❌ Handler error: {e}")

    return jsonify({"status": "ok"}), 200

# ------------------------------------------------------------
#  Entry Point
# ------------------------------------------------------------
if __name__ == "__main__":
    print("🚀 Alpaca Flask Bot – EMA5 Logic Active")
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))















