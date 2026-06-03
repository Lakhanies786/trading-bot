import asyncio
import json
import httpx
import websockets
from datetime import datetime
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from strategy.indicators import prepare_dataframe, add_indicators

PRICE_WARN_PERCENT  = 1.5
VOLUME_SPIKE_MULT   = 2.0
DANGER_ZONE_PERCENT = 0.5
CHECK_INTERVAL_SECS = 300

async def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload)
        except Exception as e:
            print(f"[Telegram Error] {e}")

class OpenTrade:
    def _init_(self, symbol, direction, entry_price,
                 stop_loss, take_profit, market="spot"):
        self.symbol      = symbol
        self.direction   = direction
        self.entry_price = entry_price
        self.stop_loss   = stop_loss
        self.take_profit = take_profit
        self.market      = market
        self.opened_at   = datetime.utcnow()
        self.is_realtime = False
        self.alerted     = set()

async def check_price_and_indicators(trade: OpenTrade) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            url = f"https://api.mexc.com/api/v3/ticker/price?symbol={trade.symbol}"
            r = await client.get(url)
            current_price = float(r.json()["price"])

        # Price movement check
        if trade.direction == "BUY":
            move_pct = ((trade.entry_price - current_price) / trade.entry_price) * 100
            dist_to_sl = ((current_price - trade.stop_loss) / current_price) * 100
        else:
            move_pct = ((current_price - trade.entry_price) / trade.entry_price) * 100
            dist_to_sl = ((trade.stop_loss - current_price) / current_price) * 100

        if move_pct >= PRICE_WARN_PERCENT and "price_warn" not in trade.alerted:
            trade.alerted.add("price_warn")
            await send_telegram(
                f"⚠️ PRICE WARNING — {trade.symbol}\n"
                f"Direction: {trade.direction}\n"
                f"Entry: {trade.entry_price} → Now: {current_price}\n"
                f"Moved {move_pct:.2f}% against you\n"
                f"Stop-Loss at: {trade.stop_loss}"
            )

        in_danger = dist_to_sl <= DANGER_ZONE_PERCENT

        # Indicator check
        async with httpx.AsyncClient() as client:
            kr = await client.get(
                f"https://api.mexc.com/api/v3/klines"
                f"?symbol={trade.symbol}&interval=15m&limit=60"
            )
            klines = kr.json()

        df = prepare_dataframe(klines)
        df = add_indicators(df)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        macd_flipped = (
            trade.direction == "BUY"
            and prev["macd"] > prev["macd_signal"]
            and last["macd"] < last["macd_signal"]
        ) or (
            trade.direction == "SELL"
            and prev["macd"] < prev["macd_signal"]
            and last["macd"] > last["macd_signal"]
        )

        rsi_danger = (
            (trade.direction == "BUY"  and last["rsi"] > 75) or
            (trade.direction == "SELL" and last["rsi"] < 25)
        )

        if (macd_flipped or rsi_danger) and "indicator_warn" not in trade.alerted:
            trade.alerted.add("indicator_warn")
            reason = "MACD flipped against trade" if macd_flipped \
                     else f"RSI extreme ({last['rsi']:.1f})"
            await send_telegram(
                f"🟠 SIGNAL REVERSAL — {trade.symbol}\n"
                f"Direction: {trade.direction}\n"
                f"⚡ {reason}\n"
                f"Current price: {current_price}\n"
                f"Consider reviewing your position!"
            )

        avg_volume  = df["volume"].iloc[-21:-1].mean()
        last_volume = df["volume"].iloc[-1]
        if last_volume >= avg_volume * VOLUME_SPIKE_MULT:
            await send_telegram(
                f"🟡 VOLUME SPIKE — {trade.symbol}\n"
                f"Spike: {last_volume/avg_volume:.1f}x normal volume\n"
                f"Unusual market activity detected!"
            )

        trade.alerted.discard("volume_spike")
        trade.alerted.discard("indicator_warn")
        return in_danger

    except Exception as e:
        print(f"[Monitor Error] {e}")
        return False

async def realtime_watch(trade: OpenTrade):
    ws_url = "wss://wbs.mexc.com/ws"
    await send_telegram(
        f"🔴 DANGER ZONE — {trade.symbol}\n"
        f"Price near stop-loss {trade.stop_loss}\n"
        f"Switching to real-time monitoring..."
    )
    try:
        async with websockets.connect(ws_url) as ws:
            sub = json.dumps({
                "method": "SUBSCRIPTION",
                "params": [f"spot@public.deals.v3.api@{trade.symbol}"]
            })
            await ws.send(sub)
            async for raw in ws:
                data = json.loads(raw)
                if "d" not in data:
                    continue
                deals = data["d"].get("deals", [])
                if not deals:
                    continue
                current_price = float(deals[0]["p"])
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
                        f"🚨 STOP-LOSS HIT — {trade.symbol}\n"
                        f"Price {current_price} crossed SL {trade.stop_loss}\n"
                        f"Close your position immediately!"
                    )
                    break
                if tp_hit:
                    await send_telegram(
                        f"✅ TAKE-PROFIT HIT — {trade.symbol}\n"
                        f"Price {current_price} reached TP {trade.take_profit}\n"
                        f"Consider closing your position!"
                    )
                    break
    except Exception as e:
        print(f"[WebSocket Error] {e}")

async def monitor_trade(trade: OpenTrade):
    await send_telegram(
        f"👁️ Monitor Started\n"
        f"Symbol: {trade.symbol}\n"
        f"Direction: {trade.direction}\n"
        f"Entry: {trade.entry_price}\n"
        f"SL: {trade.stop_loss} | TP: {trade.take_profit}"
    )
    while True:
        in_danger = await check_price_and_indicators(trade)
        if in_danger:
            await realtime_watch(trade)
            break
        await asyncio.sleep(CHECK_INTERVAL_SECS)
        