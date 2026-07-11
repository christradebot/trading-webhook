from flask import Flask, request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import StopLimitOrderRequest, ReplaceOrderRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, OrderStatus
import os
import threading
import time
from datetime import datetime

app = Flask(__name__)

trading_client = TradingClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"), paper=True)

# Tracks the highest price reached for each symbol to calculate trailing stop
high_water_marks = {}

def position_manager():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] Manager active: Breakeven + 10% Trailing.")
    while True:
        try:
            positions = trading_client.get_all_positions()
            for pos in positions:
                symbol = pos.symbol
                current_price = float(pos.current_price)
                entry_price = float(pos.avg_entry_price)
                
                # 1. Update High Water Mark
                if symbol not in high_water_marks or current_price > high_water_marks[symbol]:
                    high_water_marks[symbol] = current_price
                
                # 2. Breakeven Move (Profit >= $0.04)
                if current_price >= (entry_price + 0.04):
                    request_params = GetOrdersRequest(status=OrderStatus.OPEN)
                    orders = trading_client.get_orders(filter=request_params)
                    for order in orders:
                        if order.symbol == symbol and order.type == "stop_limit":
                            if float(order.stop_price) < entry_price:
                                trading_client.replace_order_by_id(order.id, ReplaceOrderRequest(stop_price=entry_price))
                                print(f"[{datetime.now().strftime('%H:%M:%S')}] MOVED TO BE: {symbol}")

                # 3. 10% Trailing Stop Logic (Active once above BE)
                if current_price >= (entry_price + 0.04):
                    trail_trigger = high_water_marks[symbol] * 0.90
                    # If current price drops to or below the trailing trigger, market close position
                    if current_price <= trail_trigger:
                        trading_client.close_position(symbol)
                        print(f"[{datetime.now().strftime('%H:%M:%S')}] TRAILING STOP HIT: {symbol} at ${current_price:.2f}")
                        if symbol in high_water_marks: del high_water_marks[symbol]

        except Exception as e:
            print(f"Manager error: {e}")
        time.sleep(10)

threading.Thread(target=position_manager, daemon=True).start()

@app.route("/", methods=["POST"])
def webhook():
    # ... (Keep existing webhook code here)
    return "Success", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))



























































































































