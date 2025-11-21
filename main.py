from flask import Flask, request, jsonify
import os, time, json
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestTradeRequest

# =========================
# CONFIG
# =========================
API_KEY = os.getenv("ALPACA_API_KEY")
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

app = Flask(__name__)

active_trade = {
    "symbol": None,
    "entry": None,
    "stop": None,
    "target": None,
    "qty": None,
    "in_position": False,
    "last_order_id": None
}

# =========================
# GET LIVE PRICE
# =========================

def get_last_price(symbol):
    try:
        req = StockLatestTradeRequest(symbol_or_symbols=[symbol])
        trade = data_client.get_stock_latest_trade(req)
        return float(trade[symbol].price)
    except Exception as e:
        print(f"[PRICE ERROR] {e}")
        return None

# =========================
# SAFETY CHECK
# =========================

def has_position(symbol):
    try:
        pos = trading_client.get_open_position(symbol)
        return pos is not None
    except:
        return False

# =========================
# PLACE LIMIT
# =========================

def place_limit_order(symbol, qty, price, side):
    order_request = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        limit_price=str(price),
        time_in_force=TimeInForce.DAY
    )

    try:
        order = trading_client.submit_order(order_request)
        print(f"[ORDER] {side} {qty} {symbol} @ {price}")
        return order.id
    except Exception as e:
        print(f"[ORDER ERROR] {e}")
        return None

# =========================
# OPTION A ENTRY (6 tries)
# =========================

def attempt_entry(symbol, entry, qty):
    print("[ENTRY] Starting Option A ladder...")

    for i in range(6):
        step_price = round(entry + (i * 0.01), 2)
        price = get_last_price(symbol)

        print(f"Trying buy @ {step_price} | Current: {price}")

        if price is None:
            continue

        if price <= step_price:
            order_id = place_limit_order(symbol, qty, step_price, OrderSide.BUY)
            if order_id:
                return order_id

        time.sleep(5)

    print("[ENTRY] Missed move. Protect capital.")
    return None

# =========================
# OPTION A EXIT (LADDER)
# =========================

def attempt_exit(symbol, stop, qty):
    print("[EXIT] Starting stop ladder...")

    for i in range(6):
        step_price = round(stop - (i * 0.01), 2)
        price = get_last_price(symbol)

        print(f"Trying sell @ {step_price} | Current: {price}")

        if price is None:
            continue

        if price >= step_price:
            order_id = place_limit_order(symbol, qty, step_price, OrderSide.SELL)
            if order_id:
                return order_id

        time.sleep(5)

    print("[EXIT] Aggressive final exit")
    place_limit_order(symbol, qty, round(price - 0.05, 2), OrderSide.SELL)

# =========================
# WEBHOOK
# =========================

@app.route('/webhook', methods=['POST'])
def webhook():

    try:
        payload = request.get_json()
    except:
        return jsonify({"error": "Invalid JSON"}), 400

    if payload.get("secret") != os.getenv("WEBHOOK_SECRET"):
        return jsonify({"error": "Unauthorized"}), 401

    symbol = payload.get("ticker")
    qty = int(payload.get("quantity"))
    entry = float(payload.get("entry"))
    stop = float(payload.get("stop"))
    target = float(payload.get("target"))
    action = payload.get("action")

    print(f"[WEBHOOK] {payload}")

    if action == "PLAN":

        if has_position(symbol) or active_trade["in_position"]:
            print("[BLOCK] Existing position active")
            return jsonify({"status": "blocked"})

        order_id = attempt_entry(symbol, entry, qty)

        if order_id:
            active_trade.update({
                "symbol": symbol,
                "entry": entry,
                "stop": stop,
                "target": target,
                "qty": qty,
                "in_position": True,
                "last_order_id": order_id
            })

        return jsonify({"status": "processed"})

    if action == "STOP":

        if not active_trade["in_position"]:
            return jsonify({"status": "no position"})

        attempt_exit(symbol, stop, qty)
        active_trade["in_position"] = False
        return jsonify({"status": "stopped"})

    return jsonify({"status": "ignored"})

# =========================
# HEALTH CHECK
# =========================

@app.route('/')
def home():
    return "ATHENA BOT — LIVE ✅"











































































































