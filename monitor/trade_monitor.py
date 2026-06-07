import asyncio
import json
import httpx
import websockets
from datetime import datetime
from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

PRICE_WARN_PERCENT  = 1.5
DANGER_ZONE_PERCENT = 0.5
CHECK_INTERVAL_SECS = 60   # check every 1 minute while in trade

async def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    async with httpx.AsyncClient() as client:
        try:
            await client.post(url, json=payload, timeout=5)
        except:
            pass

class OpenTrade:
    def __init__(self, symbol, direction, entry_price,
                 stop_loss, take_profit, market="spot"):
        self.symbol       = symbol
        self.direction    = direction
        self.entry_price  = entry_price
        self.stop_loss    = stop_loss
        self.take_profit  = take_profit
        self.market       = market
        self.opened_at    = datetime.utcnow()
        self.trailing_sl  = stop_loss
        self.alerted      = set()
        self.is_realtime  = False

    # ── ONLY checks price vs SL and TP ──────────────────
    # Does NOT check timeframes or indicators while in trade
    async def check_price_only(self) -> dict:
        try:
            async with httpx.AsyncClient() as client:
                url = f"https://api.mexc.com/api/v3/ticker/price?symbol={self.symbol}"
                r   = await client.get(url, timeout=5)
                current_price = float(r.json()["price"])

            direction = self.direction
            sl        = self.trailing_sl
            tp        = self.take_profit
            entry     = self.entry_price

            # Calculate profit/loss %
            if direction == "BUY":
                pnl_pct   = ((current_price - entry) / entry) * 100
                sl_hit    = current_price <= sl
                tp_hit    = current_price >= tp
                dist_to_sl = ((current_price - sl) / current_price) * 100
            else:
                pnl_pct   = ((entry - current_price) / entry) * 100
                sl_hit    = current_price >= sl
                tp_hit    = current_price <= tp
                dist_to_sl = ((sl - current_price) / current_price) * 100

            in_danger = dist_to_sl <= DANGER_ZONE_PERCENT

            # Trailing stop — move SL to breakeven when +2% profit
            if pnl_pct >= 2.0 and "trailing" not in self.alerted:
                self.alerted.add("trailing")
                self.trailing_sl = entry  # move SL to breakeven
                await send_telegram(
                    f"📈 TRAILING STOP — {self.symbol}\n"
                    f"Profit reached +{pnl_pct:.1f}%\n"
                    f"Stop loss moved to breakeven: ${entry}"
                )

            # Price warning — moved against you by 1.5%
            if pnl_pct <= -PRICE_WARN_PERCENT and "price_warn" not in self.alerted:
                self.alerted.add("price_warn")
                await send_telegram(
                    f"⚠️ PRICE WARNING — {self.symbol}\n"
                    f"Trade is -{abs(pnl_pct):.1f}% from entry\n"
                    f"Entry: ${entry}\n"
                    f"Current: ${current_price}\n"
                    f"Stop Loss: ${sl}"
                )

            return {
                "price":      current_price,
                "pnl_pct":    round(pnl_pct, 2),
                "sl_hit":     sl_hit,
                "tp_hit":     tp_hit,
                "in_danger":  in_danger,
            }

        except Exception as e:
            print(f"[Monitor Error] {e}")
            return {"sl_hit": False, "tp_hit": False, "in_danger": False}


async def realtime_watch(trade: OpenTrade):
    """Real-time WebSocket when near stop loss"""
    ws_url = "wss://wbs.mexc.com/ws"
    await send_telegram(
        f"🔴 DANGER ZONE — {trade.symbol}\n"
        f"Price very close to Stop Loss ${trade.trailing_sl}\n"
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
                price = float(deals[0]["p"])

                sl_hit = (
                    (trade.direction == "BUY"  and price <= trade.trailing_sl) or
                    (trade.direction == "SELL" and price >= trade.trailing_sl)
                )
                tp_hit = (
                    (trade.direction == "BUY"  and price >= trade.take_profit) or
                    (trade.direction == "SELL" and price <= trade.take_profit)
                )

                if sl_hit:
                    await send_telegram(
                        f"🚨 STOP LOSS HIT — {trade.symbol}\n"
                        f"Price: ${price}\n"
                        f"Stop Loss: ${trade.trailing_sl}\n"
                        f"Close your position NOW!"
                    )
                    break

                if tp_hit:
                    await send_telegram(
                        f"✅ TAKE PROFIT HIT — {trade.symbol}\n"
                        f"Price: ${price}\n"
                        f"Take Profit: ${trade.take_profit}\n"
                        f"Consider closing your position!"
                    )
                    break

    except Exception as e:
        print(f"[WebSocket Error] {e}")


async def monitor_trade(trade: OpenTrade):
    """
    Main monitor loop.
    ONLY watches price vs SL/TP.
    Does NOT change signal or show confusing messages.
    """
    entry  = trade.entry_price
    sl     = trade.stop_loss
    tp     = trade.take_profit
    risk   = abs(entry - sl)
    reward = abs(tp - entry)
    rr     = f"1:{round(reward/risk, 1)}" if risk > 0 else "1:2"

    await send_telegram(
        f"👁 TRADE MONITOR STARTED\n"
        f"Symbol: {trade.symbol}\n"
        f"Direction: {trade.direction}\n"
        f"Entry: ${entry}\n"
        f"Stop Loss: ${sl}\n"
        f"Take Profit: ${tp}\n"
        f"Risk:Reward: {rr}\n"
        f"I will alert you ONLY when:\n"
        f"• Price near stop loss\n"
        f"• Stop loss hit\n"
        f"• Take profit hit\n"
        f"• Profit reaches +2% (trailing stop)"
    )

    while True:
        result = await trade.check_price_only()

        if result.get("sl_hit"):
            pnl = result.get("pnl_pct", 0)
            await send_telegram(
                f"🚨 STOP LOSS HIT — {trade.symbol}\n"
                f"Close your position immediately!\n"
                f"Loss: {pnl:.1f}%"
            )
            break

        if result.get("tp_hit"):
            pnl = result.get("pnl_pct", 0)
            await send_telegram(
                f"✅ TAKE PROFIT HIT — {trade.symbol}\n"
                f"Excellent! Consider closing.\n"
                f"Profit: +{pnl:.1f}%"
            )
            break

        if result.get("in_danger"):
            await realtime_watch(trade)
            break

        await asyncio.sleep(CHECK_INTERVAL_SECS)