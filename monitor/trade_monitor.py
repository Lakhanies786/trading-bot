import asyncio
import json
import httpx
import websockets
from datetime import datetime
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from strategy.indicators import prepare_dataframe, add_indicators, generate_signal

PRICE_WARN_PERCENT   = 1.0   # warn at 1% move against trade (was 1.5)
VOLUME_SPIKE_MULT    = 2.0
DANGER_ZONE_PERCENT  = 0.5
CHECK_INTERVAL_SECS  = 120   # check every 2 mins (was 5 mins)
TRAILING_STEP_PCT    = 1.5
CLOSE_WARN_THRESHOLD = 2     # send EXIT warning after 2 bad signals


async def send_telegram(message: str):
    url     = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload)
        except Exception as e:
            print(f"[Telegram Error] {e}")


class OpenTrade:
    def __init__(self, symbol, direction, entry_price,
                 stop_loss, take_profit, market="spot"):

        if entry_price <= 0:
            raise ValueError(f"Invalid entry_price: {entry_price}")
        if stop_loss <= 0:
            raise ValueError(f"Invalid stop_loss: {stop_loss}")
        if take_profit <= 0:
            raise ValueError(f"Invalid take_profit: {take_profit}")
        if direction not in ("BUY", "SELL"):
            raise ValueError(f"Invalid direction: {direction}")
        if direction == "BUY" and stop_loss >= entry_price:
            raise ValueError(f"BUY trade: stop_loss {stop_loss} must be BELOW entry {entry_price}")
        if direction == "SELL" and stop_loss <= entry_price:
            raise ValueError(f"SELL trade: stop_loss {stop_loss} must be ABOVE entry {entry_price}")

        self.symbol           = symbol
        self.direction        = direction
        self.entry_price      = entry_price
        self.stop_loss        = stop_loss
        self.take_profit      = take_profit
        self.market           = market
        self.opened_at        = datetime.utcnow()
        self.is_realtime      = False
        self.alerted          = set()
        self.highest_price    = entry_price
        self.lowest_price     = entry_price
        self.trailing_sl      = stop_loss
        self.bad_signal_count = 0   # NEW: counts warning signals


async def update_trailing_stop(trade: OpenTrade, current_price: float):
    trail_pct = TRAILING_STEP_PCT / 100

    if trade.direction == "BUY":
        if current_price > trade.highest_price:
            trade.highest_price = current_price
            gain_pct = (current_price - trade.entry_price) / trade.entry_price
            if gain_pct >= trail_pct:
                new_sl = round(current_price * (1 - trail_pct), 4)
                if new_sl > trade.trailing_sl:
                    trade.trailing_sl = new_sl
                    trade.stop_loss   = new_sl
                    await send_telegram(
                        f"🔒 *TRAILING STOP MOVED* — {trade.symbol}\n"
                        f"New SL: ${new_sl}\n"
                        f"Current: ${current_price} | Gain: {gain_pct*100:.2f}%"
                    )

    elif trade.direction == "SELL":
        if current_price < trade.lowest_price:
            trade.lowest_price = current_price
            gain_pct = (trade.entry_price - current_price) / trade.entry_price
            if gain_pct >= trail_pct:
                new_sl = round(current_price * (1 + trail_pct), 4)
                if new_sl < trade.trailing_sl:
                    trade.trailing_sl = new_sl
                    trade.stop_loss   = new_sl
                    await send_telegram(
                        f"🔒 *TRAILING STOP MOVED* — {trade.symbol}\n"
                        f"New SL: ${new_sl}\n"
                        f"Current: ${current_price} | Gain: {gain_pct*100:.2f}%"
                    )


async def check_4h_trend(symbol: str, direction: str) -> tuple[bool, str]:
    """
    NEW: Check if 4h trend has reversed against our trade direction.
    Returns (reversed: bool, reason: str)
    """
    try:
        async with httpx.AsyncClient() as client:
            kr = await client.get(
                f"https://api.mexc.com/api/v3/klines"
                f"?symbol={symbol}&interval=4h&limit=100"
            )
            klines_4h = kr.json()

        df_4h  = add_indicators(prepare_dataframe(klines_4h))
        sig_4h = generate_signal(df_4h)
        last   = df_4h.iloc[-1]

        reversed_against = (
            (direction == "BUY"  and sig_4h["signal"] == "SELL") or
            (direction == "SELL" and sig_4h["signal"] == "BUY")
        )

        # Also check EMA200 on 4h — strongest trend filter
        price      = float(last["close"])
        ema200_4h  = float(last["ema200"]) if last["ema200"] > 0 else price
        below_ema200 = price < ema200_4h
        above_ema200 = price > ema200_4h

        ema200_against = (
            (direction == "BUY"  and below_ema200) or
            (direction == "SELL" and above_ema200)
        )

        # ADX direction on 4h
        adx_against = (
            (direction == "BUY"  and float(last["adx_neg"]) > float(last["adx_pos"])) or
            (direction == "SELL" and float(last["adx_pos"]) > float(last["adx_neg"]))
        )

        reasons = []
        if reversed_against:  reasons.append(f"4H signal reversed to {sig_4h['signal']}")
        if ema200_against:    reasons.append("4H price crossed against EMA200")
        if adx_against:       reasons.append("4H ADX direction flipped against trade")

        # Consider reversed if at least 2 of the 3 4h checks are against trade
        reversal_count = sum([reversed_against, ema200_against, adx_against])
        is_reversed    = reversal_count >= 2

        return is_reversed, " | ".join(reasons) if reasons else "4H trend OK"

    except Exception as e:
        print(f"[4H Check Error] {e}")
        return False, "4H check failed"


async def check_fibonacci_levels(symbol: str, direction: str, current_price: float) -> tuple[bool, str]:
    """
    NEW: Check if price broke below key Fibonacci support (BUY) or above resistance (SELL).
    This is a strong exit signal.
    """
    try:
        async with httpx.AsyncClient() as client:
            kr = await client.get(
                f"https://api.mexc.com/api/v3/klines"
                f"?symbol={symbol}&interval=1h&limit=200"
            )
            klines = kr.json()

        df = add_indicators(prepare_dataframe(klines))
        last = df.iloc[-1]

        fib_618 = float(last["fib_618"]) if last.get("fib_618", 0) else 0
        fib_500 = float(last["fib_500"]) if last.get("fib_500", 0) else 0
        fib_382 = float(last["fib_382"]) if last.get("fib_382", 0) else 0

        if direction == "BUY":
            # Price broke below 61.8% = very bearish, exit signal
            if fib_618 > 0 and current_price < fib_618:
                return True, f"⚠️ Price broke BELOW Fibonacci 61.8% support (${fib_618:.4f}) — strong exit signal"
            if fib_500 > 0 and current_price < fib_500:
                return True, f"⚠️ Price broke BELOW Fibonacci 50% support (${fib_500:.4f}) — consider exiting"
        elif direction == "SELL":
            # Price broke above 38.2% = bullish recovery, exit short signal
            if fib_382 > 0 and current_price > fib_382:
                return True, f"⚠️ Price broke ABOVE Fibonacci 38.2% resistance (${fib_382:.4f}) — strong exit signal"
            if fib_500 > 0 and current_price > fib_500:
                return True, f"⚠️ Price broke ABOVE Fibonacci 50% resistance (${fib_500:.4f}) — consider exiting"

        return False, "Fibonacci levels holding"

    except Exception as e:
        print(f"[Fibonacci Check Error] {e}")
        return False, "Fibonacci check failed"


async def check_price_and_indicators(trade: OpenTrade) -> bool:
    try:
        # ── Get current price ──────────────────────────────────────
        async with httpx.AsyncClient() as client:
            url = f"https://api.mexc.com/api/v3/ticker/price?symbol={trade.symbol}"
            r   = await client.get(url)
            current_price = float(r.json()["price"])

        await update_trailing_stop(trade, current_price)

        # ── P&L and distance to SL ────────────────────────────────
        if trade.direction == "BUY":
            move_pct   = ((trade.entry_price - current_price) / trade.entry_price) * 100
            dist_to_sl = ((current_price - trade.stop_loss) / current_price) * 100
            pnl_pct    = ((current_price - trade.entry_price) / trade.entry_price) * 100
        else:
            move_pct   = ((current_price - trade.entry_price) / trade.entry_price) * 100
            dist_to_sl = ((trade.stop_loss - current_price) / current_price) * 100
            pnl_pct    = ((trade.entry_price - current_price) / trade.entry_price) * 100

        # ── Price moving against warning ──────────────────────────
        if move_pct >= PRICE_WARN_PERCENT and "price_warn" not in trade.alerted:
            trade.alerted.add("price_warn")
            trade.bad_signal_count += 1
            await send_telegram(
                f"⚠️ *PRICE MOVING AGAINST TRADE* — {trade.symbol}\n"
                f"Direction: {trade.direction}\n"
                f"Entry: ${trade.entry_price} → Now: ${current_price}\n"
                f"Loss: {move_pct:.2f}% | PnL: {pnl_pct:.2f}%\n"
                f"Stop-Loss at: ${trade.stop_loss}\n"
                f"Distance to SL: {dist_to_sl:.2f}%"
            )

        in_danger = dist_to_sl <= DANGER_ZONE_PERCENT

        # ── 15m indicator check ───────────────────────────────────
        async with httpx.AsyncClient() as client:
            kr = await client.get(
                f"https://api.mexc.com/api/v3/klines"
                f"?symbol={trade.symbol}&interval=15m&limit=100"
            )
            klines = kr.json()

        df   = add_indicators(prepare_dataframe(klines))
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # MACD flip
        macd_flipped = (
            trade.direction == "BUY"
            and prev["macd"] > prev["macd_signal"]
            and last["macd"] < last["macd_signal"]
        ) or (
            trade.direction == "SELL"
            and prev["macd"] < prev["macd_signal"]
            and last["macd"] > last["macd_signal"]
        )

        # RSI extreme
        rsi_danger = (
            (trade.direction == "BUY"  and last["rsi"] > 75) or
            (trade.direction == "SELL" and last["rsi"] < 25)
        )

        # EMA cross against
        ema_cross_against = (
            trade.direction == "BUY"
            and prev["ema9"] >= prev["ema21"]
            and last["ema9"] < last["ema21"]
        ) or (
            trade.direction == "SELL"
            and prev["ema9"] <= prev["ema21"]
            and last["ema9"] > last["ema21"]
        )

        # CVD against trade
        cvd_against = False
        if "cvd" in df.columns and "cvd_sma" in df.columns:
            cvd_against = (
                (trade.direction == "BUY"  and float(last["cvd"]) < float(last["cvd_sma"])) or
                (trade.direction == "SELL" and float(last["cvd"]) > float(last["cvd_sma"]))
            )

        # EMA200 against trade (1h)
        ema200_against = False
        if last.get("ema200", 0) > 0:
            ema200_against = (
                (trade.direction == "BUY"  and float(last["close"]) < float(last["ema200"])) or
                (trade.direction == "SELL" and float(last["close"]) > float(last["ema200"]))
            )

        # ── NEW: 4H trend check ───────────────────────────────────
        trend_reversed, trend_reason = await check_4h_trend(trade.symbol, trade.direction)

        # ── NEW: Fibonacci level check ────────────────────────────
        fib_broken, fib_reason = await check_fibonacci_levels(
            trade.symbol, trade.direction, current_price
        )

        # ── Build warning list ────────────────────────────────────
        warning_reasons = []
        if macd_flipped:      warning_reasons.append("MACD flipped against trade")
        if rsi_danger:        warning_reasons.append(f"RSI extreme ({last['rsi']:.1f})")
        if ema_cross_against: warning_reasons.append("EMA9/21 crossed against trade")
        if cvd_against:       warning_reasons.append("CVD — real selling pressure building")
        if ema200_against:    warning_reasons.append("Price on wrong side of EMA200")
        if trend_reversed:    warning_reasons.append(f"🚨 {trend_reason}")
        if fib_broken:        warning_reasons.append(f"📐 {fib_reason}")

        # Count how many bad signals we have
        if warning_reasons:
            trade.bad_signal_count += len(warning_reasons)
        else:
            # Good signals — reduce bad count
            trade.bad_signal_count = max(0, trade.bad_signal_count - 1)

        # ── CRITICAL EXIT WARNING (NEW) ───────────────────────────
        # If 4h reversed OR fibonacci broken → immediate EXIT warning
        if trend_reversed or fib_broken:
            await send_telegram(
                f"🚨 *EXIT NOW WARNING* — {trade.symbol}\n"
                f"Direction: {trade.direction} | PnL: {pnl_pct:.2f}%\n"
                f"Current: ${current_price}\n"
                f"\n*Critical signals against your trade:*\n"
                f"{chr(10).join(warning_reasons)}\n"
                f"\n❌ *Recommend closing position to protect capital!*"
            )
            trade.alerted.discard("indicator_warn")  # allow re-alert

        # ── Standard indicator warning ────────────────────────────
        elif warning_reasons and "indicator_warn" not in trade.alerted:
            trade.alerted.add("indicator_warn")
            await send_telegram(
                f"🟠 *SIGNAL WEAKENING* — {trade.symbol}\n"
                f"Direction: {trade.direction} | PnL: {pnl_pct:.2f}%\n"
                f"Current: ${current_price}\n"
                f"⚡ {' | '.join(warning_reasons)}\n"
                f"Bad signal count: {trade.bad_signal_count}/{CLOSE_WARN_THRESHOLD}\n"
                + (
                    f"\n⚠️ *Multiple signals against you — consider closing!*"
                    if trade.bad_signal_count >= CLOSE_WARN_THRESHOLD else
                    f"\nMonitoring closely..."
                )
            )

        # ── Healthy trade update ──────────────────────────────────
        elif not warning_reasons and "healthy_update" not in trade.alerted:
            trade.alerted.add("healthy_update")
            await send_telegram(
                f"✅ *TRADE HEALTHY* — {trade.symbol}\n"
                f"Direction: {trade.direction} | PnL: {pnl_pct:.2f}%\n"
                f"Current: ${current_price}\n"
                f"SL: ${trade.stop_loss} | TP: ${trade.take_profit}\n"
                f"4H trend: {'✅ Aligned' if not trend_reversed else '❌ Reversed'}\n"
                f"All indicators aligned with trade ✅"
            )

        # Volume spike
        avg_volume  = df["volume"].iloc[-21:-1].mean()
        last_volume = df["volume"].iloc[-1]
        if last_volume >= avg_volume * VOLUME_SPIKE_MULT:
            await send_telegram(
                f"🟡 *VOLUME SPIKE* — {trade.symbol}\n"
                f"Spike: {last_volume/avg_volume:.1f}x normal volume\n"
                f"Direction: {trade.direction} | PnL: {pnl_pct:.2f}%\n"
                f"Unusual activity — watch closely!"
            )

        trade.alerted.discard("indicator_warn")
        trade.alerted.discard("healthy_update")
        return in_danger

    except Exception as e:
        print(f"[Monitor Error] {e}")
        return False


async def realtime_watch(trade: OpenTrade):
    ws_url = "wss://wbs.mexc.com/ws"
    await send_telegram(
        f"🔴 *DANGER ZONE* — {trade.symbol}\n"
        f"Price near stop-loss ${trade.stop_loss}\n"
        f"Switching to real-time tick monitoring..."
    )
    try:
        async with websockets.connect(ws_url) as ws:
            sub = json.dumps({
                "method": "SUBSCRIPTION",
                "params": [f"spot@public.deals.v3.api@{trade.symbol}"]
            })
            await ws.send(sub)
            async for raw in ws:
                data  = json.loads(raw)
                if "d" not in data:
                    continue
                deals = data["d"].get("deals", [])
                if not deals:
                    continue
                current_price = float(deals[0]["p"])

                await update_trailing_stop(trade, current_price)

                sl_hit = (
                    (trade.direction == "BUY"  and current_price <= trade.stop_loss) or
                    (trade.direction == "SELL" and current_price >= trade.stop_loss)
                )
                tp_hit = (
                    (trade.direction == "BUY"  and current_price >= trade.take_profit) or
                    (trade.direction == "SELL" and current_price <= trade.take_profit)
                )
                if sl_hit:
                    await send_telegram(
                        f"🚨 *STOP-LOSS HIT* — {trade.symbol}\n"
                        f"Price ${current_price} hit SL ${trade.stop_loss}\n"
                        f"❌ *Close your position immediately!*"
                    )
                    break
                if tp_hit:
                    await send_telegram(
                        f"✅ *TAKE-PROFIT HIT* — {trade.symbol}\n"
                        f"Price ${current_price} reached TP ${trade.take_profit}\n"
                        f"🎉 *Close and take your profits!*"
                    )
                    break
    except Exception as e:
        print(f"[WebSocket Error] {e}")


async def monitor_trade(trade: OpenTrade):
    await send_telegram(
        f"👁️ *Trade Monitor Started*\n"
        f"Symbol: {trade.symbol}\n"
        f"Direction: {trade.direction}\n"
        f"Entry: ${trade.entry_price}\n"
        f"Stop Loss: ${trade.stop_loss}\n"
        f"Take Profit: ${trade.take_profit}\n"
        f"Check interval: every {CHECK_INTERVAL_SECS//60} mins\n"
        f"Trailing stop: ACTIVE ✅\n"
        f"4H trend monitoring: ACTIVE ✅\n"
        f"Fibonacci monitoring: ACTIVE ✅"
    )
    while True:
        in_danger = await check_price_and_indicators(trade)
        if in_danger:
            await realtime_watch(trade)
            break
        await asyncio.sleep(CHECK_INTERVAL_SECS)
