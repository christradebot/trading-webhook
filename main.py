# main.py
# TradingView → Alpaca webhook for pre/regular/post-market limit entries with trade cap per ticker

from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import Optional, Literal
from datetime import datetime, timedelta, timezone, date
import alpaca_trade_api as tradeapi
import os, json, hmac, hashlib, asyncio, logging

# ─────────────────────────────────────────────────────────────────────────────
# Environment + setup
# ─────────────────────────────────────────────────────────────────────────────
ALPACA_KEY        = os.getenv("ALPACA_KEY_ID")
ALPACA_SECRET     = os.getenv("ALPACA_SECRET_KEY")
ALPACA_BASE       = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
WEBHOOK_SECRET    = os.getenv("WEBHOOK_SECRET", "")
ACCOUNT_EQUITY_CAP = float(os.getenv("ACCOUNT_EQUITY_CAP", "100000"))
MAX_RISK_PCT       = float(os.getenv("MAX_RISK_PER_TRADE_PCT", "2.0"))
MAX_PRICE_DEVIATION_BP = int(os.getenv("MAX_PRICE_DEVIATION_BP", "300"))  # 3.0%
SYMBOL_WHITELIST  = [s.strip().upper() for s in os.getenv("SYMBOL_WHITELIST", "").split(",") if s]
TRADE_LIMIT_PER_SYMBOL = int(os.getenv("TRADE_LIMIT_PER_SYMBOL", "6"))

api = tradeapi.REST(ALPACA_KEY, ALPACA_SECRET, ALPACA_BASE, api_version="v2")
app = FastAPI(title="TradingView → Alpaca Webhook")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

orders_state = {}     # signal_id -> record
trade_counter = {}    # symbol -> int
current_day = date.today()

# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────
class PositionSizing(BaseModel):
    mode: Literal["shares", "notional_pct"] = "shares"
    value: float = 100

class TVPayload(BaseModel):
    type: Literal["BUY_SIGNAL", "EXIT_SIGNAL", "STOP_SIGNAL"]
    symbol: str
    time: str
    tf: str
    bar_index: int
    close: float
    vwap: Optional[float] = None
    ema9: Optional[float] = None
    stop_ema: Optional[float] = None
    reason: Optional[str] = None
    position_sizing: Optional[PositionSizing] = None
    signal_id: str

# ─────────────────────────────────────────────────────────────────────────────
# Utility functions
# ─────────────────────────────────────────────────────────────────────────────
def verify_hmac(req: Request, raw: bytes):
    if not WEBHOOK_SECRET:
        return
    sig = req.headers.get("X-Signature", "")
    mac = hmac.new(WEBHOOK_SECRET.encode(), msg=raw, digestmod=hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, mac):
        raise HTTPException(status_code=401, detail="Invalid HMAC signature")

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def tf_minutes(tf: str) -> int:
    try:
        return 1440 if tf.upper() in ("D", "1D") else int(tf)
    except:
        return 5

def clamp_qty(symbol: str, px: float, sizing: PositionSizing) -> int:
    acct = api.get_account()
    eq = min(float(acct.equity), ACCOUNT_EQUITY_CAP)
    if sizing.mode == "notional_pct":
        notional = eq * (sizing.value / 100)
        raw_qty = int(notional // px)
    else:
        raw_qty = int(sizing.value)
    max_notional = eq * (MAX_RISK_PCT / 100)
    max_qty = int(max_notional // px)
    return max(1, min(raw_qty, max_qty))

def price_guard(ref: float, candidate: float):
    dev_bp = abs(candidate - ref) / ref * 10000 if ref else 0
    if dev_bp > MAX_PRICE_DEVIATION_BP:
        raise HTTPException(status_code=422, detail=f"Limit {dev_bp:.1f}bp away from ref")
    return candidate

# Reset daily trade counts at UTC midnight
def reset_trade_counter_if_new_day():
    global current_day
    if date.today() != current_day:
        trade_counter.clear()
        current_day = date.today()
        logging.info("Trade counters reset for new day")

# ─────────────────────────────────────────────────────────────────────────────
# Background sweeper (expiry for unfilled orders)
# ─────────────────────────────────────────────────────────────────────────────
async def sweep_expired():
    while True:
        try:
            reset_trade_counter_if_new_day()
            for sig_id, rec in list(orders_state.items()):
                if rec.get("side") == "buy" and "expiry_at" in rec:
                    expiry = datetime.fromisoformat(rec["expiry_at"])
                    if now_utc() > expiry:
                        o = api.get_order(rec["alpaca_id"])
                        if o.filled_qty and float(o.filled_qty) < float(o.qty):
                            api.cancel_order(o.id)
                            logging.info(f"Cancelled expired order {o.id}")
                        orders_state.pop(sig_id, None)
        except Exception as e:
            logging.warning(f"Sweeper error: {e}")
        await asyncio.sleep(60)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(sweep_expired())

# ─────────────────────────────────────────────────────────────────────────────
# Core webhook endpoint
# ─────────────────────────────────────────────────────────────────────────────
@app.post("/tv")
async def tv(request: Request):
    raw = await request.body()
    verify_hmac(request, raw)
    try:
        p = TVPayload(**json.loads(raw))
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")

    reset_trade_counter_if_new_day()

    symbol = p.symbol.upper()
    if SYMBOL_WHITELIST and symbol not in SYMBOL_WHITELIST:
        raise HTTPException(status_code=403, detail=f"{symbol} not allowed")

    if p.signal_id in orders_state:
        return {"status": "duplicate_ignored"}

    # enforce per-ticker trade limit
    count = trade_counter.get(symbol, 0)
    if count >= TRADE_LIMIT_PER_SYMBOL:
        raise HTTPException(status_code=403, detail=f"{symbol} reached {TRADE_LIMIT_PER_SYMBOL}-trade limit")

    if p.type == "BUY_SIGNAL":
        levels = [x for x in [p.ema9, p.vwap] if x and x > 0]
        target = min(levels) if levels else p.close
        target = price_guard(p.close, target)
        sizing = p.position_sizing or PositionSizing()
        qty = clamp_qty(symbol, target, sizing)
        expiry = now_utc() + timedelta(minutes=tf_minutes(p.tf) * 5)
        try:
            o = api.submit_order(
                symbol=symbol,
                side="buy",
                type="limit",
                time_in_force="day",
                limit_price=round(target, 2),
                qty=qty,
                extended_hours=True
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        orders_state[p.signal_id] = {
            "alpaca_id": o.id,
            "symbol": symbol,
            "side": "buy",
            "qty": qty,
            "limit": float(target),
            "expiry_at": expiry.isoformat()
        }
        trade_counter[symbol] = trade_counter.get(symbol, 0) + 1
        logging.info(f"BUY {symbol} {qty}@{target} | trade {trade_counter[symbol]}/{TRADE_LIMIT_PER_SYMBOL}")
        return {"status": "buy_submitted", "symbol": symbol, "qty": qty, "limit": target}

    elif p.type in ("EXIT_SIGNAL", "STOP_SIGNAL"):
        try:
            pos = api.get_position(symbol)
            qty = int(float(pos.qty))
        except Exception:
            return {"status": "no_position"}
        limit_px = round(p.close * 0.995, 2)
        limit_px = price_guard(p.close, limit_px)
        try:
            o = api.submit_order(
                symbol=symbol,
                side="sell",
                type="limit",
                time_in_force="gtc",
                limit_price=limit_px,
                qty=qty,
                extended_hours=True
            )
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))
        orders_state[p.signal_id] = {
            "alpaca_id": o.id,
            "symbol": symbol,
            "side": "sell",
            "qty": qty,
            "limit": float(limit_px)
        }
        trade_counter[symbol] = trade_counter.get(symbol, 0) + 1
        logging.info(f"EXIT {symbol} {qty}@{limit_px} | trade {trade_counter[symbol]}/{TRADE_LIMIT_PER_SYMBOL}")
        return {"status": "exit_submitted", "symbol": symbol, "qty": qty, "limit": limit_px}

    raise HTTPException(status_code=400, detail="Unknown signal type")












