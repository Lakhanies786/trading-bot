import time
import requests
import os
from typing import Optional

PAIRS               = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]
ACCOUNT_BALANCE     = 1000
RISK_PER_TRADE      = 0.015
MAX_OPEN_TRADES     = 3
MIN_CONFIDENCE      = 70
MIN_SCORE           = 10
MAX_DAILY_LOSS_PCT  = 3.0
MAX_CONSECUTIVE_LOSSES = 3
TRAILING_STOP_PCT   = 1.5


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
        self.open_trades        = {}
        self.trade_history      = []
        self.daily_pnl          = 0.0
        self.daily_reset_time   = time.time()
        self.consecutive_losses = 0

    def _reset_daily_if_needed(self):
        if time.time() - self.daily_reset_time > 86400:
            self.daily_pnl          = 0.0
            self.daily_reset_time   = time.time()
            self.consecutive_losses = 0
            send_telegram("📅 Daily P&L reset. Trading resumed for new day.")

    def _is_daily_loss_limit_hit(self) -> bool:
        self._reset_daily_if_needed()
        if self.daily_pnl <= -MAX_DAILY_LOSS_PCT:
            send_telegram(
                f"🛑 DAILY LOSS LIMIT HIT ({self.daily_pnl:.2f}%)\n"
                f"Bot paused for rest of day to protect your account."
            )
            return True
        return False

    def _calc_position_size(self, price: float, stop_loss: float) -> float:
        risk_amount = ACCOUNT_BALANCE * RISK_PER_TRADE
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            risk_amount *= 0.5
            send_telegram(
                f"⚠️ {self.consecutive_losses} consecutive losses. "
                f"Position size halved for protection."
            )
        risk_per_unit = abs(price - stop_loss)
        if risk_per_unit == 0:
            return 0
        return round(risk_amount / risk_per_unit, 6)

    def get_signal(self, symbol: str) -> Optional[dict]:
        try:
            from mexc.client import MEXCSpotClient
            from strategy.indicators import prepare_dataframe, add_indicators, generate_signal
            from strategy.orderbook import analyze_orderbook

            spot = MEXCSpotClient()

            klines_15m = spot.get_klines(symbol, interval="15m", limit=200)
            klines_1h  = spot.get_klines(symbol, interval="1h",  limit=200)
            klines_4h  = spot.get_klines(symbol, interval="4h",  limit=200)

            df_15m = add_indicators(prepare_dataframe(klines_15m))
            df_1h  = add_indicators(prepare_dataframe(klines_1h))
            df_4h  = add_indicators(prepare_dataframe(klines_4h))

            sig_15m = generate_signal(df_15m)
            sig_1h  = generate_signal(df_1h)
            sig_4h  = generate_signal(df_4h)

            ob     = spot.get_orderbook(symbol, limit=50)
            ob_res = analyze_orderbook(ob, sig_1h["price"])

            sig              = sig_1h.copy()
            sig["symbol"]    = symbol
            sig["ob_signal"] = ob_res["ob_signal"]
            sig["sig_15m"]   = sig_15m["signal"]
            sig["sig_4h"]    = sig_4h["signal"]

            signals    = [sig_15m["signal"], sig_1h["signal"], sig_4h["signal"]]
            buy_count  = signals.count("BUY")
            sell_count = signals.count("SELL")
            sig["mtf_agreement"] = buy_count if sig["signal"] == "BUY" else sell_count

            # Fibonacci levels from 4h for stronger S/R
            last_4h = df_4h.iloc[-1]
            sig["fib_618_4h"] = float(last_4h.get("fib_618", 0))
            sig["fib_500_4h"] = float(last_4h.get("fib_500", 0))
            sig["fib_382_4h"] = float(last_4h.get("fib_382", 0))

            return sig
        except Exception as e:
            print(f"Signal error for {symbol}: {e}")
            return None

    def should_take_trade(self, sig: dict) -> bool:
        if self._is_daily_loss_limit_hit():
            return False
        if sig["signal"] == "HOLD":
            return False
        if len(self.open_trades) >= MAX_OPEN_TRADES:
            return False
        if sig["symbol"] in self.open_trades:
            return False

        score_num = int(sig.get("score", "0/16").split("/")[0])
        if score_num < MIN_SCORE:
            return False

        conf_num = int(sig.get("confidence", "0%").replace("%", ""))
        if conf_num < MIN_CONFIDENCE:
            return False

        # Require at least 2/3 timeframes to agree
        if sig.get("mtf_agreement", 0) < 2:
            return False

        # ✅ FIX: NEVER trade against the 4H trend
        if sig["signal"] == "BUY" and sig.get("sig_4h") == "SELL":
            send_telegram(
                f"⛔ Skipping BUY on {sig['symbol']} — 4H says SELL\n"
                f"Trading against 4H trend is too risky."
            )
            return False
        if sig["signal"] == "SELL" and sig.get("sig_4h") == "BUY":
            send_telegram(
                f"⛔ Skipping SELL on {sig['symbol']} — 4H says BUY\n"
                f"Trading against 4H trend is too risky."
            )
            return False

        # Don't trade against strong orderbook
        ob = sig.get("ob_signal", "NEUTRAL")
        if sig["signal"] == "BUY"  and ob == "SELL":
            return False
        if sig["signal"] == "SELL" and ob == "BUY":
            return False

        # ✅ NEW: Don't buy above Fibonacci 38.2% resistance or sell below 61.8% support
        price    = sig["price"]
        fib_382  = sig.get("fib_382_4h", 0)
        fib_618  = sig.get("fib_618_4h", 0)

        if sig["signal"] == "BUY" and fib_382 > 0 and price > fib_382:
            send_telegram(
                f"⛔ Skipping BUY on {sig['symbol']} — price above Fibonacci 38.2% resistance (${fib_382:.4f})\n"
                f"Poor risk/reward from this level."
            )
            return False
        if sig["signal"] == "SELL" and fib_618 > 0 and price < fib_618:
            send_telegram(
                f"⛔ Skipping SELL on {sig['symbol']} — price below Fibonacci 61.8% support (${fib_618:.4f})\n"
                f"Poor risk/reward from this level."
            )
            return False

        return True

    def place_order(self, symbol: str, sig: dict) -> bool:
        try:
            from mexc.client import MEXCSpotClient
            spot  = MEXCSpotClient()
            price = sig["price"]
            side  = sig["signal"]
            sl    = sig["stop_loss"]
            tp    = sig["take_profit"]
            qty   = self._calc_position_size(price, sl)

            if sl is None or tp is None or qty == 0:
                return False

            order = spot.place_order(
                symbol=symbol, side=side,
                order_type="MARKET", quantity=round(qty, 6)
            )

            if order.get("orderId"):
                self.open_trades[symbol] = {
                    "direction":     side,
                    "entry_price":   price,
                    "stop_loss":     sl,
                    "take_profit":   tp,
                    "quantity":      qty,
                    "order_id":      order["orderId"],
                    "opened_at":     time.time(),
                    "highest_price": price,
                    "lowest_price":  price,
                    "trailing_sl":   sl,
                }
                send_telegram(
                    f"✅ AUTO TRADE PLACED\n"
                    f"Symbol: {symbol} | Direction: {side}\n"
                    f"Entry: ${price} | SL: ${sl} | TP: ${tp}\n"
                    f"Size: {qty} | Score: {sig['score']}\n"
                    f"Confidence: {sig['confidence']}\n"
                    f"15m={sig.get('sig_15m')} 1h={sig['signal']} 4h={sig.get('sig_4h')}\n"
                    f"Fib 38.2%=${sig.get('fib_382_4h', 'N/A')} | 61.8%=${sig.get('fib_618_4h', 'N/A')}"
                )
                return True
        except Exception as e:
            print(f"Order error: {e}")
            send_telegram(f"❌ Order failed for {symbol}: {e}")
        return False

    def _update_trailing_stop(self, symbol: str, trade: dict, price: float):
        direction = trade["direction"]
        entry     = trade["entry_price"]
        trail_pct = TRAILING_STOP_PCT / 100

        if direction == "BUY":
            if price > trade["highest_price"]:
                trade["highest_price"] = price
                gain_pct = (price - entry) / entry
                if gain_pct >= trail_pct:
                    new_sl = round(price * (1 - trail_pct), 4)
                    if new_sl > trade["trailing_sl"]:
                        trade["trailing_sl"] = new_sl
                        trade["stop_loss"]   = new_sl
                        send_telegram(
                            f"🔒 TRAILING STOP — {symbol}\n"
                            f"New SL: ${new_sl} | Price: ${price}"
                        )
        elif direction == "SELL":
            if price < trade["lowest_price"]:
                trade["lowest_price"] = price
                gain_pct = (entry - price) / entry
                if gain_pct >= trail_pct:
                    new_sl = round(price * (1 + trail_pct), 4)
                    if new_sl < trade["trailing_sl"]:
                        trade["trailing_sl"] = new_sl
                        trade["stop_loss"]   = new_sl
                        send_telegram(
                            f"🔒 TRAILING STOP — {symbol}\n"
                            f"New SL: ${new_sl} | Price: ${price}"
                        )

    def monitor_open_trades(self):
        for symbol, trade in list(self.open_trades.items()):
            try:
                sig = self.get_signal(symbol)
                if not sig:
                    continue

                price     = sig["price"]
                direction = trade["direction"]

                self._update_trailing_stop(symbol, trade, price)

                sl = trade["stop_loss"]
                tp = trade["take_profit"]

                sl_hit = (direction == "BUY"  and price <= sl) or \
                         (direction == "SELL" and price >= sl)
                tp_hit = (direction == "BUY"  and price >= tp) or \
                         (direction == "SELL" and price <= tp)

                # ✅ FIX: Exit if 4H reverses
                four_h_reversed = (
                    (direction == "BUY"  and sig.get("sig_4h") == "SELL") or
                    (direction == "SELL" and sig.get("sig_4h") == "BUY")
                )

                if sl_hit:
                    self.close_trade(symbol, price, "STOP LOSS HIT")
                elif tp_hit:
                    self.close_trade(symbol, price, "TAKE PROFIT HIT")
                elif four_h_reversed:
                    self.close_trade(symbol, price, "4H TREND REVERSED")
                elif sig["signal"] not in (direction, "HOLD"):
                    self.close_trade(symbol, price, "SIGNAL REVERSED")

            except Exception as e:
                print(f"Monitor error {symbol}: {e}")

    def close_trade(self, symbol: str, price: float, reason: str):
        trade = self.open_trades.pop(symbol, None)
        if not trade:
            return
        try:
            from mexc.client import MEXCSpotClient
            spot       = MEXCSpotClient()
            close_side = "SELL" if trade["direction"] == "BUY" else "BUY"
            spot.place_order(
                symbol=symbol, side=close_side,
                order_type="MARKET", quantity=round(trade["quantity"], 6)
            )
        except Exception as e:
            print(f"Close error: {e}")

        pnl     = (price - trade["entry_price"]) if trade["direction"] == "BUY" \
                  else (trade["entry_price"] - price)
        pnl_pct = round((pnl / trade["entry_price"]) * 100, 2)

        self.daily_pnl += pnl_pct
        if pnl_pct < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

        self.trade_history.append({
            "symbol":  symbol,
            "pnl_pct": pnl_pct,
            "reason":  reason
        })

        emoji = "✅" if pnl > 0 else "❌"
        send_telegram(
            f"{emoji} TRADE CLOSED — {symbol}\n"
            f"Reason: {reason}\n"
            f"Entry: ${trade['entry_price']} | Exit: ${price}\n"
            f"PnL: {pnl_pct}% | Daily PnL: {self.daily_pnl:.2f}%"
        )

    def scan_and_trade(self):
        if self._is_daily_loss_limit_hit():
            return
        for symbol in PAIRS:
            sig = self.get_signal(symbol)
            if sig and self.should_take_trade(sig):
                self.place_order(symbol, sig)
        self.monitor_open_trades()


auto_trader = AutoTrader()
