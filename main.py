@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        data = request.get_json(force=True)
        print("🚀 Incoming webhook data:", data, flush=True)

        if not data:
            return jsonify({"status": "no data"}), 400

        action = data.get("action")
        symbol = data.get("symbol")
        qty = float(data.get("qty", 0))

        if action == "buy":
            api.submit_order(symbol=symbol, qty=qty, side='buy', type='market', time_in_force='gtc')
            print(f"✅ Buy order placed for {qty} {symbol}", flush=True)
        elif action == "sell":
            api.submit_order(symbol=symbol, qty=qty, side='sell', type='market', time_in_force='gtc')
            print(f"✅ Sell order placed for {qty} {symbol}", flush=True)
        else:
            print("⚠️ Unknown action:", action, flush=True)

        return jsonify({"status": "ok"}), 200

    except Exception as e:
        print("❌ Error in webhook:", e, flush=True)
        return jsonify({"status": "error", "message": str(e)}), 500



