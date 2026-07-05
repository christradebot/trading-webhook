import os
from flask import Flask, request, jsonify
from dotenv import load_dotenv 

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import StopLimitOrderRequest
from alpaca.trading.enums import OrderSide, OrderClass, TimeInForce

load_dotenv()

app = Flask(__name__)

# Configuration
API_KEY = os.getenv("APCA_API_KEY_ID")
SECRET_KEY = os.getenv("APCA_API_SECRET_KEY")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")
BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")

paper = "paper" in BASE_URL

client = TradingClient(API_KEY, SECRET_KEY, paper=paper)

@app.route("/", methods=["GET"])
def home():
    return "Trading Bot Running" 

@app.route("/tv", methods=["POST"])
def webhook():
    data = request.json
    
    # Debugging: Print received data to Railway Logs
    print(f"Received alert data: {data}")

    # Security check
    if data.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorized"}), 403

    try:
        # Construct the bracket order
        order = StopLimitOrderRequest(
            symbol=data["symbol"],
            qty=int(data["qty"]),
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            stop_price=float(data["buy_stop"]),
            limit_price=float(data["buy_limit"]),
            order_class=OrderClass.BRACKET,
            take_profit={"limit_price": float(data["take_profit"])},
            stop_loss={"stop_price": float(data["stop_loss"])}
        )

        response = client.submit_order(order)
        
        return jsonify({
            "success": True,
            "id": response.id
        })

    except Exception as e:
        print(f"Order error: {e}")
        return jsonify({
            "success": False,
            "error": str(e)
        }), 400

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)



























































































































