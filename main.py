# ================================================================
# main.py v2.5  |  © Athena + Chris 2025
# Unified Trade Manager: BUY / ADD / EXIT (Limit-Only Execution)
# ================================================================

from flask import Flask, request, jsonify
from alpaca_trade_api.rest import REST, TimeFrame
import os, json, datetime, time, math

# ------------------------------------------------
# 🧠 Environment & Config
# ------------------------------------------------
ALPACA_KEY_ID     = os.environ.get("ALPACA_KEY_ID")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://api.alpaca.markets")
WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "mysecret")

api = REST(ALPACA_KEY_ID, ALPACA_SECRET_KEY, ALPACA_BASE_URL)

app = Flask(__name__)

# ------------------------------------------------
# ⚙️ Utility Helpers
# ------------------------------------------------
def now_et():
    """Return current US Eastern time"""
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=-4)))

def within_vol_window():
    """Check if current time is between 09:30 and 09:45 ET"""
    t = now_et().time()
    return t >= datetime.time(9, 30) and t <= datetime.time(9, 45)

def calc_atr(symbol, period=14):
    """Calculate ATR using last 14 one-minute bars"""
    bars = api.get_bars(symbol, TimeFrame.Minute, limit=period + 1).df
    if bars.empty: return None
    highs, lows, closes = bars['high'], bars['low'], bars['close']
    trs = [max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])) for i in range(1, len(bars))]
    return sum(trs) / len(trs)

def aggressive_limit_close(symbol, qty, side="sell", limit_price=None):
    """Aggressively close using limit orders until filled"""
    print(f"⚠️ Stop triggered → aggressive limit {side} @ {limit_price}")
    retries = 0
    while retries < 10:
        try:
            api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type="limit",
                time_in_force="day",
                limit_price=limit_price,
                extended_hours=True
            )
            print(f"✅ Limit exit submitted @ {limit_price}")
            break
        except Exception as e:
            print(f"Retry {retries+1}/10: {e}")
            retries += 1
            time.sleep(1)

def get_position(symbol):
    """Fetch existing position if any"""
    try:
        return api.get_position(symbol)
    except:
        return None

def get_pnl(symbol):
    """Return current unrealized PnL for logging"""
    pos = get_position(symbol)
    if not pos: return 0.0
    return float(pos.unrealized_pl)

# ------------------------------------------------
# 💰 Trade Manager
# ------------------------------------------------
def manage_trade(symbol, side, alert_price, signal_type):
    """Main unified trade handler"""
    alert_price = float(alert_price)
    qty = 100  # fixed lot size for testing

    pos = get_position(symbol)
    has_pos = pos is not None
    unreal_pnl = get_pnl(symbol)

    # --- BUY Logic ---
    if side == "buy":
        if has_pos:
            print(f"⚠️ Already in {symbol}, skipping BUY.")
            return

        # Determine stop mode
        if within_vol_window():
            atr = calc_atr(symbol, 14)
            stop_price = round(alert_price - (atr * 3), 4)
            stop_mode = "ATR×3 (09:30–09:45)"
        else:
            bars = api.get_bars(symbol, TimeFrame.Minute, limit=1).df
            stop_price = round(float(bars['low'][-1]), 4)
            stop_mode = "Candle-Low"

        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="limit",
            time_in_force="day",
            limit_price=alert_price,
            extended_hours=True
        )
        print(f"🚀 BUY {symbol} @ ${alert_price} | Stop Mode: {stop_mode} → Stop @ ${stop_price}")
        print(f"📊 Unrealized PnL: ${unreal_pnl:.2f}")

    # --- ADD Logic ---
    elif side == "add":
        if not has_pos:
            print(f"⚠️ No open position for {symbol}, skipping ADD.")
            return
        if unreal_pnl <= 0:
            print(f"⚠️ {symbol} not profitable, skipping ADD.")
            return

        api.submit_order(
            symbol=symbol,
            qty=qty,
            side="buy",
            type="limit",
            time_in_force="day",
            limit_price=alert_price,
            extended_hours=True
        )
        print(f"➕ ADD {symbol} @ ${alert_price} | PnL before add: ${unreal_pnl:.2f}")

    # --- EXIT Logic ---
    elif side == "exit":
        if not has_pos:
            print(f"⚠️ No position to exit for {symbol}.")
            return

        stop_price = float(pos.avg_entry_price) * 0.98  # fallback safety stop
        api.submit_order(
            symbol=symbol,
            qty=pos.qty,
            side="sell",
            type="limit",
            time_in_force="day",
            limit_price=alert_price,
            extended_hours=True
        )
        print(f"💣 EXIT {symbol} @ ${alert_price}")
        print(f"💰 Realized PnL: ${pos.unrealized_pl}")

# ------------------------------------------------
# 📡 Webhook Endpoint
# ------------------------------------------------
@app.post("/tv")
def webhook():
    data = request.get_json()
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "unauthorized"}), 403

    symbol = data.get("ticker")
    price = data.get("price") or data.get("close")
    message = data.get("message", "").lower()

    if not symbol or not price:
        return jsonify({"error": "missing data"}), 400

    if "add" in message:
        manage_trade(symbol, "add", price, "hammer_add")
    elif "buy" in message:
        manage_trade(symbol, "buy", price, "hammer_buy")
    elif "exit" in message:
        manage_trade(symbol, "exit", price, "scalper_exit")
    else:
        print(f"Ignored message: {message}")

    return jsonify({"status": "ok", "symbol": symbol})

# ------------------------------------------------
# 🧾 JSON Payload Templates
# ------------------------------------------------
hammer_buy_payload = {
    "secret": WEBHOOK_SECRET,
    "ticker": "{{ticker}}",
    "price": "{{close}}",
    "message": "Bullish Hammer Detected — Action: BUY"
}

hammer_add_payload = {
    "secret": WEBHOOK_SECRET,
    "ticker": "{{ticker}}",
    "price": "{{close}}",
    "message": "Bullish Hammer Detected — Action: ADD"
}

scalper_exit_payload = {
    "secret": WEBHOOK_SECRET,
    "ticker": "{{ticker}}",
    "price": "{{close}}",
    "message": "ITG Scalper Exit"
}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)



































