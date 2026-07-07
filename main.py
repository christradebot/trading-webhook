from flask import Flask, request
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import StopLimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
import os

app = Flask(__name__)

# Initialize Alpaca Client
api_key = os.getenv("APCA_API_KEY_ID")
secret_key = os.getenv("APCA_API_SECRET_KEY")
trading_client = TradingClient(api_key, secret_key, paper=True) 

@app.route("/", methods=["GET", "POST"])
def webhook():
    # Use force=True to ensure JSON is parsed even if header is missing
    data = request.get_json(force=True)
    
    if request.method == "POST":
        # 1. Authentication Check
        if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
            print("Unauthorized attempt!")
            return "Unauthorized", 401
            
        # 2. Extract data correctly (matching your TradingView payload)
        symbol = data.get("symbol") # Changed from 'ticker' to 'symbol'
        qty = float(data.get("qty", 0))
        buy_stop = float(data.get("buy_stop", 0))
        buy_limit = float(data.get("buy_limit", 0))
        take_profit = float(data.get("take_profit", 0))
        stop_loss = float(data.get("stop_loss", 0))
        
        print(f"DEBUG: Received Alert - Symbol: {symbol}, Qty: {qty}")

        if not symbol:
            return "Missing Symbol", 400

        # 3. Bracket Order Execution
        order_data = StopLimitOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            type="stop_limit",
            stop_price=buy_stop,
            limit_price=buy_limit,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit={"limit_price": take_profit},
            stop_loss={"stop_price": stop_loss}
        )
        
        trading_client.submit_order(order_data)
        print(f"Successfully placed bracket order for {symbol}")
        
        return "Success", 200
        
    return "Bot is running", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))



























































































































