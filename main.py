# ============================================================
#  ChrisBot Trading Webhook (Restored + Finalized)
#  - Limit orders only (pre-market & RTH via extended_hours)
#  - Immediate action on alert (no next bar delay)
#  - Entry buffer: +$0.03 (>= $1), +$0.003 (< $1)
#  - SELL: try signal_close first, then aggressive limit chase
#  - Max 2 losses per ticker per day (lockout)
#  - Auto close all at 19:59 ET
#  - Detailed logging
# ============================================================

import os, time, json
from datetime import datetime
from flask import Flask, request, jsonify

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest

# ----------------------- ENV VARS ---------------------------
API_KEY        = os.getenv("APCA_API_KEY_ID")
SECRET_KEY     = os.getenv("APCA_API_SECRET_KEY")
BASE_URL       = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "CHRISBOT1501")

if not API_KEY or not SECRET_KEY:
    raise ValueError("🚨 Alpaca API_KEY or SECRET_KEY not found in Railway Variables.")

# -------------------- ALPACA CLIENTS ------------------------
trading_client = TradingClient(API_KEY, SECRET_KEY, paper=True)
data_client    = StockHistoricalDataClient(API_KEY, SECRET_KEY)

app = Flask(__name__)

# -------------------- IN-MEMORY STATE -----------------------
# track avg entry to determine losses; track loss counts per ticker
avg_entry   = {}   # symbol -> float
open_qty    = {}   # symbol -> float
loss_count  = {}   # symbol -> int (resets on process restart)
LOCKOUT_MAX = 2    # max losses per ticker per day

# -------------------- HELPERS -------------------------------
def now_utc():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

def price_buffer(p: float) -> float:
    return 0.03 if p >= 1.0 else 0.003

def live_ref_price(symbol: str, action: str, fallback: float) -> float:
    """
    BUY uses ask, SELL uses bid. Fallback to provided price if no quote.
    """
    try:
        q = data_client.get_stock_latest_quote(StockLatestQuoteRequest(symbol=symbol))
        ref = q.ask_price if action == "BUY" else q.bid_price
        if ref is None or ref == 0:
            print(f"⚠️ {symbol} no live quote; fallback={fallback}")
            return float(fallback or 0.0)
        return float(ref)
    except Exception as e:
        print(f"⚠️ Quote error {symbol}: {e} | fallback={fallback}")
        return float(fallback or 0.0)

def submit_limit(symbol: str, qty: float, side: OrderSide, limit_price: float, tag: str):
    """
    Limit order with DAY and extended hours allowed.
    """
    try:
        order = trading_client.submit_order(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side,
                limit_price=round(limit_price, 4),
                time_in_force=TimeInForce.DAY,
                extended_hours=True,
            )
        )
        print(f"✅ {side.value.upper()} {symbol} x{qty} @ {round(limit_price,4)} [{tag}]")
        return order
    except Exception as e:
        print(f"❌ submit_limit error {side.value.upper()} {symbol}: {e} [{tag}]")
        return None

def place_buy(symbol: str, qty: float, signal_close: float, source: str):
    """
    BUY immediately using live ask + buffer. Record avg_entry/open_qty for loss tracking.
    """
    ref  = live_ref_price(symbol, "BUY", signal_close)
    buf  = price_buffer(ref)
    px   = ref + buf
    ord_ = submit_limit(symbol, qty, OrderSide.BUY, px, f"BUY|{source}|ref={ref}|buf={buf}")
    if ord_:
        avg_entry[symbol] = float(px)
        open_qty[symbol]  = open_qty.get(symbol, 0.0) + float(qty)

def try_sell_at_close(symbol: str, qty: float, signal_close: float):
    """
    First attempt: place a SELL limit at the signal candle's close (slightly inside to improve fill).
    """
    if signal_close <= 0:
        return False
    # shade inside by half-buffer to increase fill probability
    shade = price_buffer(signal_close) * 0.5
    target = max(0.0001, signal_close - shade)
    print(f"🎯 First SELL target for {symbol}: signal_close={signal_close} → target={round(target,4)}")
    ord_ = submit_limit(symbol, qty, OrderSide.SELL, target, "SELL|TARGET_CLOSE")
    # wait briefly; if still holding after grace, we'll chase
    time.sleep(5)
    return True  # attempt made (not guaranteed filled)

def remaining_position(symbol: str) -> float:
    try:
        positions = trading_client.get_all_positions()
        p = next((p for p in positions if p.symbol == symbol), None)
        return float(p.qty) if p else 0.0
    except Exception as e:
        print(f"⚠️ remaining_position error {symbol}: {e}")
        return 0.0

def chase_sell(symbol: str, base_qty: float, signal_close: float):
    """
    Aggressive limit chase until position goes to zero.
    Strategy:
      1) Start at live bid - buffer
      2) If not filled, tighten 4 more times every 5s
      3) Then repeat cycles (up to 20 tries total)
    """
    tries = 0
    while tries < 20:
        qty_left = remaining_position(symbol)
        if qty_left <= 0:
            print(f"✅ {symbol} fully closed during chase.")
            return True

        ref    = live_ref_price(symbol, "SELL", signal_close)
        buf    = price_buffer(ref)
        target = max(0.0001, ref - buf)  # aggressive to hit bid
        tag    = f"SELL|CHASE t{tries+1} ref={ref} buf={buf}"

        submit_limit(symbol, qty_left, OrderSide.SELL, target, tag)
        time.sleep(5)
        tries += 1

    # final check
    if remaining_position(symbol) > 0:
        print(f"⚠️ {symbol} not fully closed after chase loop.")
        return False
    print(f"✅ {symbol} closed at end of chase loop.")
    return True

def handle_sell_and_loss_count(symbol: str, signal_close: float):
    """
    Execute SELL flow: target signal_close, then chase. Update loss counter if sold below avg_entry.
    """
    qty_left = remaining_position(symbol)
    if qty_left <= 0:
        print(f"ℹ️ No open qty for {symbol} at SELL.")
        return

    tried = try_sell_at_close(symbol, qty_left, signal_close)
    # Whether or not first attempt fills, proceed to chase loop
    closed = chase_sell(symbol, qty_left, signal_close)

    # loss accounting vs recorded avg_entry
    try:
        sell_ref = live_ref_price(symbol, "SELL", signal_close)
        ae = float(avg_entry.get(symbol, 0.0))
        if ae and sell_ref < ae:
            loss_count[symbol] = loss_count.get(symbol, 0) + 1
            print(f"📉 LOSS counted for {symbol}: {sell_ref} < {ae} | losses={loss_count[symbol]}")
        # reset position memory if flat
        if remaining_position(symbol) <= 0:
            open_qty[symbol] = 0.0
    except Exception as e:
        print(f"⚠️ loss accounting error {symbol}: {e}")

def enforce_eod_close():
    # 19:59 ET ≈ 23:59 UTC (ignoring DST differences for simplicity)
    utc = datetime.utcnow()
    if utc.hour == 23 and utc.minute >= 59:
        print("🕘 EOD: closing all positions...")
        try:
            # Chase-logic is better per symbol, but as a safety net:
            trading_client.close_all_positions()
            print("✅ EOD close_all_positions fired.")
        except Exception as e:
            print(f"⚠️ EOD close error: {e}")

# ----------------------- WEBHOOK ----------------------------
@app.route("/tv", methods=["POST"])
def tv():
    try:
        payload = request.get_json()
        print(f"\n🔍 {now_utc()} Raw webhook:\n{json.dumps(payload, indent=2)}")

        if payload.get("secret") != WEBHOOK_SECRET:
            return jsonify({"error": "Unauthorized webhook secret"}), 401

        action       = str(payload.get("action", "")).upper()
        symbol       = str(payload.get("ticker", "")).upper()
        qty          = float(payload.get("quantity", 0))
        signal_close = float(payload.get("signal_close", 0) or 0)
        source       = str(payload.get("source", "unknown"))

        if not symbol or "{" in symbol:
            return jsonify({"error": "Invalid or placeholder ticker", "ok": False}), 400
        if qty <= 0:
            return jsonify({"error": "Quantity must be > 0", "ok": False}), 400

        # Lockout after 2 losses per ticker per day
        lc = loss_count.get(symbol, 0)
        if action == "BUY" and lc >= LOCKOUT_MAX:
            msg = f"⛔ BUY blocked for {symbol}: loss lockout (losses={lc}/{LOCKOUT_MAX})"
            print(msg)
            return jsonify({"ok": False, "blocked": True, "reason": "loss_lockout"}), 200

        print(f"✅ Parsed: action={action} symbol={symbol} qty={qty} source={source} close={signal_close}")

        if action == "BUY":
            place_buy(symbol, qty, signal_close, source)

        elif action == "SELL":
            handle_sell_and_loss_count(symbol, signal_close)

        else:
            return jsonify({"error": "Invalid action"}), 400

        enforce_eod_close()

        return jsonify({"ok": True, "symbol": symbol, "time": now_utc()}), 200

    except Exception as e:
        print(f"❌ Exception /tv: {e}")
        return jsonify({"error": str(e)}), 500

# ----------------------- HEALTH -----------------------------
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": now_utc()})

# ------------------------ RUN -------------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)












































































