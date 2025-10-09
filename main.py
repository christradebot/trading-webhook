from flask import Flask, request, jsonify
import os

app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    return "Webhook is running ✅"

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json()
    print("Received:", data)
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))
