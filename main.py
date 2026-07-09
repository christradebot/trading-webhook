from flask import Flask, request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import StopLimitOrderRequest, ReplaceOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
import os
import threading
import time

app = Flask(__name__)

# Initialize Alpaca Client
trading_client = TradingClient(os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY"), paper=True)

# --- Background Manager Thread ---
def position_manager():
    print("Manager thread started...")
    while True:
        try:
            positions = trading_client.get_all_positions()
            for pos in positions:
                # Calculate current profit vs entry
                current_price = float(pos.current_price)
                entry_price = float(pos.avg_entry_price)
                
                # Check if profit >= $0.04 AND we are not already at breakeven
                # We check if the current stop_price is still below entry_price
                if current_price >= (entry_price + 0.04):
                    # Fetch orders to find the associated Stop Loss order
                    orders = trading_client.get_orders(status="open")
                    for order in orders:
                        if order.symbol == pos.symbol and order.type == "stop_limit":
                            # Replace existing Stop Loss with new price at Entry
                            req = ReplaceOrderRequest(stop_price=entry_price)
                            trading_client.replace_order_by_id(order_id=order.id, order_data=req)
                            print(f"MOVED STOP TO BE: {pos.symbol} at {entry_price}")
        except Exception as e:
            print(f"Manager error: {e}")
        time.sleep(10) # Checks every 10 seconds

# Start the management thread
threading.Thread(target=position_manager, daemon=True).start()

# --- Webhook Entry ---
@app.route("/", methods=["POST"])
def webhook():
    data = request.get_json(force=True)
    if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
        return "Unauthorized", 401

    # Submit Bracket Order (Entry, Stop Loss, Take Profit)
    order_data = StopLimitOrderRequest(
        symbol=data.get("symbol"),
        qty=float(data.get("qty")),
        side=OrderSide.BUY,
        type="stop_limit",
        stop_price=float(data.get("buy_stop")),
        limit_price=float(data.get("buy_limit")),
        time_in_force=TimeInForce.GTC,
        order_class=OrderClass.BRACKET,
        take_profit={"limit_price": float(data.get("take_profit"))},
        stop_loss={"stop_price": float(data.get("stop_loss"))}
    )
    
    trading_client.submit_order(order_data)
    return "Success", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))



























































































































