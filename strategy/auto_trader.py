import time
import requests
import os
from typing import Optional

# Pairs to scan
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

# Risk settings
ACCOUNT_BALANCE   = 1000   # Update this to your real balance
RISK_PER_TRADE    = 0.015  # 1.5% per trade
MAX_OPEN_TRADES   = 3      # Max simultaneous trades
MIN_CONFIDENCE    = 70     # Minimum confidence % to take trade (raised from 60)
MIN_SCORE         = 8      # Minimum score out of 12 (raised from 7)

def send_telegram(message: str):
    token   = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=5
        )
    except:
        pass

class AutoTrader:

    def __init__(self):
        self.open_trades  = {}   # symbol -> trade info
        self.trade_history = []  # closed trades

    def get_signal(self, symbol: str) -> Optional[dict]:
        try:
            from mexc.client import MEXCSpotClient
            from strategy.indicators import prepare_dataframe, add_indicators, generate_signal
            from strategy.orderbook import analyze_orderbook

            spot = MEXCSpotClient()
            klines = spot.get_klines(symbol, interval="15m", limit=200)
            df     = add_indicators(prepare_dataframe(klines))
            sig    = generate_signal(df)
            ob     = spot.get_orderbook(symbol, limit=50)
            ob_res = analyze_orderbook(ob, sig["price"])
            sig["ob_signal"] = ob_res["ob_signal"]
            sig["symbol"]    = symbol
            return sig
        except Exception as e:
            print(f"Signal error for {symbol}: {e}")
            return None

    def should_take_trade(self, sig: dict) -> bool:
        if sig["signal"] == "HOLD":
            return False
        if len(self.open_trades) >= MAX_OPEN_TRADES:
            return False
        if sig["symbol"] in self.open_trades:
            return False
        score_num = int(sig.get("score", "0/12").split("/")[0])
        if score_num < MIN_SCORE:
            return False
        conf_num = int(sig.get("confidence", "0%").replace("%", ""))
        if conf_num < MIN_CONFIDENCE:
            return False
        return True

    def place_order(self, symbol: str, sig: dict) -> bool:
        try:
            from mexc.client import MEXCSpotClient
            spot  = MEXCSpotClient()
            price = sig["price"]
            side  = sig["signal"]  # BUY or SELL
            sl    = sig["stop_loss"]
            tp    = sig["take_profit"]
            qty   = sig["position_size"]

            # Safety guard — never place order if levels are missing
            if sl is None or tp is None or qty == 0:
                print(f"⚠️ Skipping {symbol} — SL/TP/qty missing")
                return False

            # Place market order
            order = spot.place_order(
                symbol=symbol,
                side=side,
                order_type="MARKET",
                quantity=round(qty, 6)
            )

            if order.get("orderId"):
                self.open_trades[symbol] = {
                    "direction":   side,
                    "entry_price": price,
                    "stop_loss":   sl,
                    "take_profit": tp,
                    "quantity":    qty,
                    "order_id":    order["orderId"],
                    "opened_at":   time.time()
                }
                send_telegram(
                    f"✅ AUTO TRADE PLACED\n"
                    f"Symbol: {symbol}\n"
                    f"Direction: {side}\n"
                    f"Entry: ${price}\n"
                    f"Stop Loss: ${sl}\n"
                    f"Take Profit: ${tp}\n"
                    f"Size: {qty}\n"
                    f"Score: {sig['score']}\n"
                    f"Confidence: {sig['confidence']}"
                )
                return True
        except Exception as e:
            print(f"Order error: {e}")
            send_telegram(f"❌ Order failed for {symbol}: {e}")
        return False

    def monitor_open_trades(self):
        for symbol, trade in list(self.open_trades.items()):
            try:
                sig = self.get_signal(symbol)
                if not sig:
                    continue

                price     = sig["price"]
                direction = trade["direction"]
                sl        = trade["stop_loss"]
                tp        = trade["take_profit"]

                # Check stop loss
                sl_hit = (direction == "BUY"  and price <= sl) or \
                         (direction == "SELL" and price >= sl)

                # Check take profit
                tp_hit = (direction == "BUY"  and price >= tp) or \
                         (direction == "SELL" and price <= tp)

                # Check signal reversal
                reversed_signal = (
                    (direction == "BUY"  and sig["signal"] == "SELL") or
                    (direction == "SELL" and sig["signal"] == "BUY")
                )

                if sl_hit:
                    self.close_trade(symbol, price, "STOP LOSS HIT")
                elif tp_hit:
                    self.close_trade(symbol, price, "TAKE PROFIT HIT")
                elif reversed_signal:
                    self.close_trade(symbol, price, "SIGNAL REVERSED")

            except Exception as e:
                print(f"Monitor error {symbol}: {e}")

    def close_trade(self, symbol: str, price: float, reason: str):
        trade = self.open_trades.pop(symbol, None)
        if not trade:
            return
        try:
            from mexc.client import MEXCSpotClient
            spot      = MEXCSpotClient()
            direction = trade["direction"]
            close_side = "SELL" if direction == "BUY" else "BUY"
            spot.place_order(
                symbol=symbol,
                side=close_side,
                order_type="MARKET",
                quantity=round(trade["quantity"], 6)
            )
        except Exception as e:
            print(f"Close error: {e}")

        pnl = (price - trade["entry_price"]) if trade["direction"] == "BUY" \
              else (trade["entry_price"] - price)
        pnl_pct = round((pnl / trade["entry_price"]) * 100, 2)

        self.trade_history.append({
            "symbol":  symbol,
            "pnl_pct": pnl_pct,
            "reason":  reason
        })

        emoji = "✅" if pnl > 0 else "❌"
        send_telegram(
            f"{emoji} TRADE CLOSED — {symbol}\n"
            f"Reason: {reason}\n"
            f"Entry: ${trade['entry_price']}\n"
            f"Exit: ${price}\n"
            f"PnL: {pnl_pct}%"
        )

    def scan_and_trade(self):
        print(f"Scanning {len(PAIRS)} pairs...")
        for symbol in PAIRS:
            sig = self.get_signal(symbol)
            if sig and self.should_take_trade(sig):
                print(f"Signal found: {symbol} {sig['signal']}")
                self.place_order(symbol, sig)
        self.monitor_open_trades()

# Global instance
auto_trader = AutoTrader()