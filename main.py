from flask import Flask, request, jsonify
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, StopLimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
import os

app = Flask(__name__)

# Initialize Alpaca Client
# Ensure these variables are set in your Railway project settings
api_key = os.getenv("APCA_API_KEY_ID")
secret_key = os.getenv("APCA_API_SECRET_KEY")
trading_client = TradingClient(api_key, secret_key, paper=True) # Set paper=False for Live

@app.route("/", methods=["GET", "POST"])  # Updated to accept POST requests
def webhook():
    if request.method == "POST":
        data = request.json
        
        # 1. Authentication Check
        if data.get("secret") != os.getenv("WEBHOOK_SECRET"):
            return "Unauthorized", 401
            
        # 2. Logic to handle the incoming TradingView alert
        # Example: Extract ticker and order info here
        symbol = data.get("ticker")
        side = OrderSide.BUY if data.get("side") == "buy" else OrderSide.SELL
        
        # 3. Position Check (Prevent duplicates)
        # Implementation logic to check if position already exists
        
        # 4. Bracket Order Execution
        # Using StopLimitOrderRequest as per your requirements
        
        print(f"Received alert for {symbol}") # Debug logging
        return "Success", 200
        
    return "Bot is running", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))



























































































































