//@version=6
indicator("Simple Breakout Webhook", overlay=true)

//====================================================
// USER INPUTS
//====================================================
secret            = input.string("CHRISBOT1501", "Webhook Secret")
buy_stop          = input.float(5.50, "Buy Stop Trigger")
limit_buffer_pct  = input.float(2.0, "Limit Buffer % (above Buy Stop)", minval=0.01, step=0.1)
take_profit       = input.float(6.10, "Take Profit")
stop_pct          = input.float(2.1, "Stop Loss % (below Buy Limit)", minval=0.01, step=0.1)

//====================================================
// DERIVED BUY LIMIT
// Sized as a % above buy_stop rather than a fixed cent amount, so the
// trigger-to-limit gap scales proportionally with price (2% is ~1.5c on a
// $1.50 stock, ~40c on a $40 stock) instead of being negligible on
// high-priced names or oversized on low-priced ones.
//====================================================
raw_buy_limit = buy_stop * (1 + limit_buffer_pct / 100)
tick_bl       = buy_stop >= 1.0 ? 0.01 : 0.0001
buy_limit     = math.round(raw_buy_limit / tick_bl) * tick_bl

//====================================================
// DERIVED STOP LOSS
// Sized off buy_limit (the worst price you can be filled at on a stop-limit
// buy), not buy_stop - so real risk never exceeds stop_pct regardless of
// where within the stop-to-limit range the fill actually lands.
//
// Rounded to the correct tick size (Reg NMS Rule 612 / Alpaca's own rule):
// stocks priced $1.00+ must be in whole-cent increments (2dp); only sub-$1
// stocks are allowed sub-penny pricing (4dp). Raw % math almost never lands
// on a clean increment, so this rounding is required, not optional.
//====================================================
raw_stop_loss = buy_limit * (1 - stop_pct / 100)
tick_sl       = buy_limit >= 1.0 ? 0.01 : 0.0001
stop_loss     = math.round(raw_stop_loss / tick_sl) * tick_sl

//====================================================

var bool orderSent = false
var float last_buy_stop = na

// Re-arms whenever you manually raise buy_stop to a new level - this is
// what actually allows a same-day re-entry on this ticker: set a fresh
// higher trigger for the next leg once the previous position has closed.
// The daily reset is kept too, as a safety net for the next session.
isNewDay = time("D") != time("D")[1]
buy_stop_changed = last_buy_stop != buy_stop
if isNewDay or buy_stop_changed
    orderSent := false
last_buy_stop := buy_stop

hourNY = hour(time, "America/New_York")
minuteNY = minute(time, "America/New_York")
after940 = hourNY > 9 or (hourNY == 9 and minuteNY >= 40)

buySignal = not orderSent and after940 and high >= buy_stop

plot(buy_stop, color=color.green, linewidth=2, title="Buy Stop")
plot(take_profit, color=color.blue, title="Take Profit")
plot(stop_loss, color=color.red, title="Stop Loss")

if buySignal
    orderSent := true
    alert(
      "{"
      + "\"secret\":\"" + secret + "\","
      + "\"symbol\":\"" + syminfo.ticker + "\","
      + "\"buy_stop\":" + str.tostring(buy_stop) + ","
      + "\"buy_limit\":" + str.tostring(buy_limit) + ","
      + "\"take_profit\":" + str.tostring(take_profit) + ","
      + "\"stop_loss\":" + str.tostring(stop_loss)
      + "}",
      alert.freq_once_per_bar
    )



























































































































