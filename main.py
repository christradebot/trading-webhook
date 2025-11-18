@app.route("/tv", methods=["POST"])
def tv_webhook():
    try:
        raw = request.data.decode("utf-8")
        log(f"📩 RAW INCOMING: {raw}")

        data = json.loads(raw)

    except Exception as e:
        log(f"❌ JSON PARSE ERROR: {e}")
        return jsonify({"status": "error", "message": "invalid_json"}), 400

    # ---------------------------
    # 1. Validate secret
    # ---------------------------
    secret = data.get("secret", "")
    if secret != WEBHOOK_SECRET:
        log(f"❌ INVALID SECRET: {secret}")
        return jsonify({"status": "error", "message": "bad_secret"}), 403

    # ---------------------------
    # 2. Read action
    # ---------------------------
    action = str(data.get("action", "")).upper()
    log(f"🔍 ACTION RECEIVED: {action}")

    # ---------------------------
    # 3. PLAN MODE — SAFE & NO ORDERS
    # ---------------------------
    if action == "PLAN":
        log("📝 PLAN MODE — NO ORDERS WILL BE SENT")
        log(f"PLAN DATA: {json.dumps(data, indent=2)}")
        return jsonify({
            "status": "ok",
            "message": "plan_ok",
            "detail": "No live orders were placed."
        }), 200

    # ---------------------------
    # 4. Extract required fields
    # ---------------------------
    ticker = data.get("ticker")
    qty = data.get("quantity")
    entry = data.get("entry")
    stop = data.get("stop")
    target = data.get("target")

    missing = []
    for k, v in {"ticker": ticker, "qty": qty, "entry": entry, "stop": stop, "target": target}.items():
        if v is None:
            missing.append(k)

    if missing:
        log(f"❌ MISSING FIELDS: {missing}")
        return jsonify({"status": "error", "message": "missing_fields", "fields": missing}), 400

    # ---------------------------
    # 5. Handle BUY
    # ---------------------------
    if action == "BUY":
        log(f"🚀 BUY REQUEST for {ticker} qty={qty} entry={entry}")

        success, err = place_entry_limit(ticker, qty, entry)

        if not success:
            log(f"❌ ENTRY ORDER FAILED: {err}")
            return jsonify({"status": "error", "message": "entry_order_failed"}), 500

        log("✅ ENTRY ORDER PLACED")

        # We do **NOT** send stop or target now
        # They only fire AFTER price hits those levels
        return jsonify({"status": "ok", "message": "entry_order_placed"}), 200

    # ---------------------------
    # 6. Handle STOP trigger
    # ---------------------------
    if action == "STOP":
        log(f"🛑 STOP TRIGGERED for {ticker} @ {stop}")

        success, err = place_stop_exit(ticker, qty, stop)

        if not success:
            log(f"❌ STOP ORDER FAILED: {err}")
            return jsonify({"status": "error", "message": "stop_order_failed"}), 500

        return jsonify({"status": "ok", "message": "stop_order_placed"}), 200

    # ---------------------------
    # 7. Handle TARGET trigger
    # ---------------------------
    if action == "TARGET":
        log(f"🎯 TARGET TRIGGERED for {ticker} @ {target}")

        success, err = place_target_exit(ticker, qty, target)

        if not success:
            log(f"❌ TARGET ORDER FAILED: {err}")
            return jsonify({"status": "error", "message": "target_order_failed"}), 500

        return jsonify({"status": "ok", "message": "target_order_placed"}), 200

    # ---------------------------
    # 8. Unknown action
    # ---------------------------
    log(f"❓ UNKNOWN ACTION: {action}")
    return jsonify({"status": "error", "message": "unknown_action"}), 400





























































































