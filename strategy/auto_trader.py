import time
import requests
import os
from typing import Optional

# ── Pairs to scan ─────────────────────────────────────────────────────
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

# ── Risk settings ─────────────────────────────────────────────────────
ACCOUNT_BALANCE    = 1000    # Update to your real balance
RISK_PER_TRADE     = 0.015   # 1.5% risk per trade
MAX_OPEN_TRADES    = 3       # Max simultaneous trades
MIN_CONFIDENCE     = 70      # Minimum confidence % to take trade
MIN_SCORE          = 10      # Minimum score out of 16 (raised from 8)

# ── NEW: Drawdown & Daily Loss Protection ─────────────────────────────
MAX_DAILY_LOSS_PCT = 3.0     # Stop trading if daily loss exceeds 3%
MAX_CONSECUTIVE_LOSSES = 3   # Reduce size after 3 losses in a row
TRAILING_STOP_PCT  = 1.5     # Trailing stop: lock in profits at 1.5% move


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
        self.open_trades       = {}    # symbol -> trade info
        self.trade_history     = []    # closed trades
        self.daily_pnl         = 0.0   # tracks today's P&L %
        self.daily_reset_time  = time.time()
        self.consecutive_losses = 0    # tracks losing streak

    # ── Daily loss reset ──────────────────────────────────────────────
    def _reset_daily_if_needed(self):
        if time.time() - self.daily_reset_time > 86400:  # 24 hours
            self.daily_pnl        = 0.0
            self.daily_reset_time = time.time()
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

    # ── Dynamic position sizing (NEW) ─────────────────────────────────
    def _calc_position_size(self, price: float, stop_loss: float) -> float:
        """
        Risk a fixed % of account per trade.
        Reduces size after consecutive losses (drawdown protection).
        """
        risk_amount = ACCOUNT_BALANCE * RISK_PER_TRADE

        # Halve size after 3 consecutive losses
        if self.consecutive_losses >= MAX_CONSECUTIVE_LOSSES:
            risk_amount *= 0.5
            send_telegram(
                f"⚠️ Consecutive losses: {self.consecutive_losses}. "
                f"Position size halved for protection."
            )

        risk_per_unit = abs(price - stop_loss)
        if risk_per_unit == 0:
            return 0
        qty = round(risk_amount / risk_per_unit, 6)
        return qty

    def get_signal(self, symbol: str) -> Optional[dict]:
        try:
            from mexc.client import MEXCSpotClient
            from strategy.indicators import prepare_dataframe, add_indicators, generate_signal
            from strategy.orderbook import analyze_orderbook

            spot = MEXCSpotClient()

            # Multi-timeframe signals (15m, 1h, 4h)
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

            # Use 1h as primary, enrich with multi-timeframe context
            sig = sig_1h.copy()
            sig["symbol"]    = symbol
            sig["ob_signal"] = ob_res["ob_signal"]
            sig["sig_15m"]   = sig_15m["signal"]
            sig["sig_4h"]    = sig_4h["signal"]

            # Agreement bonus: if all 3 timeframes agree, boost confidence
            signals = [sig_15m["signal"], sig_1h["signal"], sig_4h["signal"]]
            buy_count  = signals.count("BUY")
            sell_count = signals.count("SELL")
            sig["mtf_agreement"] = buy_count if sig["signal"] == "BUY" else sell_count

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

        score_str = sig.get("score", "0/16")
        score_num = int(score_str.split("/")[0])
        if score_num < MIN_SCORE:
            return False

        conf_num = int(sig.get("confidence", "0%").replace("%", ""))
        if conf_num < MIN_CONFIDENCE:
            return False

        # NEW: Require at least 2/3 timeframes to agree
        if sig.get("mtf_agreement", 0) < 2:
            return False

        # NEW: Don't trade against strong orderbook
        ob = sig.get("ob_signal", "NEUTRAL")
        if sig["signal"] == "BUY" and ob == "SELL":
            return False
        if sig["signal"] == "SELL" and ob == "BUY":
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

            # Dynamic position sizing
            qty = self._calc_position_size(price, sl)

            if sl is None or tp is None or qty == 0:
                print(f"⚠️ Skipping {symbol} — SL/TP/qty missing")
                return False

            order = spot.place_order(
                symbol=symbol,
                side=side,
                order_type="MARKET",
                quantity=round(qty, 6)
            )

            if order.get("orderId"):
                self.open_trades[symbol] = {
                    "direction":      side,
                    "entry_price":    price,
                    "stop_loss":      sl,
                    "take_profit":    tp,
                    "quantity":       qty,
                    "order_id":       order["orderId"],
                    "opened_at":      time.time(),
                    "highest_price":  price,   # for trailing stop (BUY)
                    "lowest_price":   price,   # for trailing stop (SELL)
                    "trailing_sl":    sl,      # updated as price moves favorably
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
                    f"Confidence: {sig['confidence']}\n"
                    f"Timeframes: 15m={sig.get('sig_15m')} 1h={sig['signal']} 4h={sig.get('sig_4h')}"
                )
                return True
        except Exception as e:
            print(f"Order error: {e}")
            send_telegram(f"❌ Order failed for {symbol}: {e}")
        return False

    def _update_trailing_stop(self, symbol: str, trade: dict, price: float):
        """
        NEW: Trailing Stop Loss.
        For BUY: move SL up as price rises, lock in profits.
        For SELL: move SL down as price falls.
        """
        direction    = trade["direction"]
        entry        = trade["entry_price"]
        trail_pct    = TRAILING_STOP_PCT / 100

        if direction == "BUY":
            # Only trail if price moved up by at least trail_pct
            if price > trade["highest_price"]:
                trade["highest_price"] = price
                gain_pct = (price - entry) / entry
                if gain_pct >= trail_pct:
                    new_sl = round(price * (1 - trail_pct), 4)
                    if new_sl > trade["trailing_sl"]:
                        trade["trailing_sl"] = new_sl
                        trade["stop_loss"]   = new_sl
                        send_telegram(
                            f"🔒 TRAILING STOP UPDATED — {symbol}\n"
                            f"New SL: ${new_sl} (locked in profit)\n"
                            f"Current price: ${price}"
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
                            f"🔒 TRAILING STOP UPDATED — {symbol}\n"
                            f"New SL: ${new_sl} (locked in profit)\n"
                            f"Current price: ${price}"
                        )

    def monitor_open_trades(self):
        for symbol, trade in list(self.open_trades.items()):
            try:
                sig = self.get_signal(symbol)
                if not sig:
                    continue

                price     = sig["price"]
                direction = trade["direction"]

                # Update trailing stop
                self._update_trailing_stop(symbol, trade, price)

                sl = trade["stop_loss"]   # may have been updated by trailing stop
                tp = trade["take_profit"]

                sl_hit = (direction == "BUY"  and price <= sl) or \
                         (direction == "SELL" and price >= sl)
                tp_hit = (direction == "BUY"  and price >= tp) or \
                         (direction == "SELL" and price <= tp)

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
            spot       = MEXCSpotClient()
            direction  = trade["direction"]
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

        # Update daily P&L and streak trackers
        self.daily_pnl += pnl_pct
        if pnl_pct < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0   # reset on a win

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
            f"PnL: {pnl_pct}%\n"
            f"Daily PnL: {self.daily_pnl:.2f}%\n"
            f"Consecutive losses: {self.consecutive_losses}"
        )

    def scan_and_trade(self):
        if self._is_daily_loss_limit_hit():
            print("Daily loss limit hit — skipping scan")
            return
        print(f"Scanning {len(PAIRS)} pairs...")
        for symbol in PAIRS:
            sig = self.get_signal(symbol)
            if sig and self.should_take_trade(sig):
                print(f"Signal found: {symbol} {sig['signal']}")
                self.place_order(symbol, sig)
        self.monitor_open_trades()


# Global instance
auto_trader = AutoTrader()
