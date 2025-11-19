            if active_plan["entry_filled"] and active_plan["trail_active"] and not active_plan["trail_sent"]:
                trail_pct = active_plan["trail_pct"]
                if trail_pct is None or trail_pct <= 0:
                    # No trail configured
                    active_plan["trail_active"] = False
                else:
                    # Update highest bid
                    if active_plan["highest_bid"] is None or bid > active_plan["highest_bid"]:
                        active_plan["highest_bid"] = bid

                    highest = active_plan["highest_bid"]
                    trail_level = highest * (1.0 - trail_pct / 100.0)

                    log(f"[TRAIL] highest={highest:.4f}, trail_level={trail_level:.4f}, bid={bid:.4f}")

                    if bid <= trail_level:
                        log("TRAILING STOP HIT → sending SELL")

                        if has_open_position(symbol):
                            order = submit_limit_order(
                                symbol=symbol,
                                qty=active_plan["qty"],
                                price=bid,  # trail exits at current bid
                                side=OrderSide.SELL
                            )
                            if order is not None:
                                active_plan["trail_sent"] = True
                                active_plan["in_position"] = False
                                active_plan["trail_active"] = False
                                log("Trailing stop sent, position closed. (S3: other exits logically cancelled)")
                        else:
                            log("[TRAIL EXIT] No open position at Alpaca, skipping SELL.")

            time.sleep(1)

        except Exception as loop_err:
            log(f"[MONITOR ERROR] {loop_err}")
            traceback.print_exc()
            time.sleep(2)

# =====================================================================
# WEBHOOK ENDPOINT
# =====================================================================

@app.route("/tv", methods=["POST"])
def tv_webhook():
    payload = parse_webhook_payload(request)

    if payload is None:
        return jsonify({"status": "error", "message": "invalid_json"}), 400

    # Validate secret
    if str(payload.get("secret")) != str(WEBHOOK_SECRET):
        log("[ERROR] SECRET INVALID")
        return jsonify({"status": "error", "message": "bad_secret"}), 401

    log("SECRET VALID")

    # Extract and validate fields
    try:
        ticker = str(payload["ticker"]).upper()
        qty = int(payload["quantity"])
        entry = float(payload["entry"])
        stop = float(payload["stop"])
        target = float(payload["target"])
        trail_pct = float(payload.get("trail_pct", 0))

        if qty <= 0:
            raise ValueError("Quantity must be > 0")
        if entry <= 0 or stop <= 0 or target <= 0:
            raise ValueError("Prices must be > 0")
        if not (stop < entry < target):
            log(f"[PAYLOAD WARNING] stop={stop}, entry={entry}, target={target} look unusual.")

    except Exception as e:
        log(f"[PAYLOAD ERROR] {e}")
        traceback.print_exc()
        return jsonify({"status": "error", "message": "bad_payload"}), 400

    # Store plan (reset all flags)
    active_plan.update({
        "ticker": ticker,
        "qty": qty,
        "entry": entry,
        "stop": stop,
        "target": target,
        "trail_pct": trail_pct,
        "in_position": False,
        "entry_filled": False,
        "stop_sent": False,
        "target_sent": False,
        "trail_sent": False,
        "trail_active": False,
        "highest_bid": None
    })

    log(f"PLAN STORED: {active_plan}")

    return jsonify({"status": "ok", "message": "plan_loaded"}), 200

# =====================================================================
# START MONITOR THREAD
# =====================================================================

import threading
threading.Thread(target=monitor_price, daemon=True).start()

# =====================================================================
# RUN FLASK
# =====================================================================

if __name__ == "__main__":
    log(f"SERVER STARTED — LIVE_MODE={LIVE_MODE}, "
        f"TRADE_UTC_WINDOW={TRADE_START_UTC_HOUR}-{TRADE_END_UTC_HOUR}, "
        f"MAX_SPREAD_PCT={MAX_SPREAD_PCT}")
    app.run(host="0.0.0.0", port=8080)

































































































