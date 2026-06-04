import asyncio
import os
from fastapi import FastAPI

app = FastAPI(title="MEXC Trading Bot", version="4.0.0")

def get_spot():
    from mexc.client import MEXCSpotClient
    return MEXCSpotClient()

active_trades: dict = {}

@app.get("/")
def root():
    return {"status": "Bot is running ✅", "version": "4.0.0"}

@app.get("/health")
def health():
    try:
        spot  = get_spot()
        price = spot.get_ticker("BTCUSDT")
        return {"status": "ok ✅", "btc_price": price.get("price")}
    except Exception as e:
        return {"error": str(e)}

@app.get("/price/{symbol}")
def get_price(symbol: str):
    try:
        return get_spot().get_ticker(symbol)
    except Exception as e:
        return {"error": str(e)}

@app.get("/account/spot")
def get_spot_account():
    try:
        return get_spot().get_account()
    except Exception as e:
        return {"error": str(e)}

@app.get("/signal/{symbol}")
def get_signal(symbol: str = "BTCUSDT"):
    try:
        from strategy.indicators import (
            prepare_dataframe, add_indicators, generate_signal
        )
        from strategy.orderbook import analyze_orderbook

        spot = get_spot()

        # 3 timeframes
        klines_15m = spot.get_klines(symbol, interval="15m", limit=100)
        klines_1h  = spot.get_klines(symbol, interval="1h",  limit=100)
        klines_4h  = spot.get_klines(symbol, interval="4h",  limit=100)

        df_15m = add_indicators(prepare_dataframe(klines_15m))
        df_1h  = add_indicators(prepare_dataframe(klines_1h))
        df_4h  = add_indicators(prepare_dataframe(klines_4h))

        sig_15m = generate_signal(df_15m)
        sig_1h  = generate_signal(df_1h)
        sig_4h  = generate_signal(df_4h)

        # Order book
        orderbook    = spot.get_orderbook(symbol, limit=50)
        ob_analysis  = analyze_orderbook(orderbook, sig_1h["price"])
        ob_signal    = ob_analysis["ob_signal"]

        # Multi timeframe agreement
        signals    = [sig_15m["signal"], sig_1h["signal"], sig_4h["signal"]]
        buy_count  = signals.count("BUY")
        sell_count = signals.count("SELL")

        if buy_count >= 2:
            mtf_signal     = "BUY"
            mtf_confidence = "HIGH" if buy_count == 3 else "MEDIUM"
            mtf_agreement  = f"✅ {buy_count}/3 timeframes say BUY"
        elif sell_count >= 2:
            mtf_signal     = "SELL"
            mtf_confidence = "HIGH" if sell_count == 3 else "MEDIUM"
            mtf_agreement  = f"✅ {sell_count}/3 timeframes say SELL"
        else:
            mtf_signal     = "HOLD"
            mtf_confidence = "LOW"
            mtf_agreement  = "⏳ Timeframes disagree — wait"

        # Final signal combining MTF + orderbook
        if mtf_signal == ob_signal and mtf_signal != "HOLD":
            final_signal   = mtf_signal
            final_strength = f"🔥 STRONG {mtf_signal} — Indicators + Order Book confirm!"
        elif mtf_signal in ("BUY", "SELL"):
            final_signal   = mtf_signal
            final_strength = f"✅ {mtf_signal} — Indicators agree"
        else:
            final_signal   = "HOLD"
            final_strength = "⏳ HOLD — No clear direction"

        main = sig_1h
        all_reasons = [final_strength, mtf_agreement] + ob_analysis["ob_reasons"] + main["reasons"]

        return {
            "symbol":            symbol,
            "signal":            final_signal,
            "confidence":        main.get("confidence", "0%"),
            "mtf_confidence":    mtf_confidence,
            "agreement":         final_strength,
            "timeframes": {
                "15m": sig_15m["signal"],
                "1h":  sig_1h["signal"],
                "4h":  sig_4h["signal"],
            },
            "ob_signal":         ob_signal,
            "bid_ask_ratio":     ob_analysis["bid_ask_ratio"],
            "total_bid_volume":  ob_analysis["total_bid_volume"],
            "total_ask_volume":  ob_analysis["total_ask_volume"],
            "biggest_buy_wall":  ob_analysis["biggest_buy_wall"],
            "biggest_sell_wall": ob_analysis["biggest_sell_wall"],
            "buy_wall_price":    ob_analysis["buy_wall_price"],
            "sell_wall_price":   ob_analysis["sell_wall_price"],
            "price":             main["price"],
            "rsi":               main["rsi"],
            "macd_hist":         main["macd_hist"],
            "ema9":              main["ema9"],
            "ema21":             main["ema21"],
            "ema50":             main["ema50"],
            "bb_lower":          main["bb_lower"],
            "bb_upper":          main["bb_upper"],
            "atr":               main["atr"],
            "adx":               main["adx"],
            "stop_loss":         main["stop_loss"],
            "take_profit":       main["take_profit"],
            "position_size":     main["position_size"],
            "risk_reward":       main["risk_reward"],
            "reasons":           all_reasons,
        }
# Telegram alert for BUY/SELL
        if final_signal in ("BUY", "SELL"):
            try:
                import requests as req
                token = os.getenv("TELEGRAM_BOT_TOKEN")
                chat_id = os.getenv("TELEGRAM_CHAT_ID")
                msg = (
                    f"SIGNAL {final_signal} - {symbol}\n"
                    f"Price: {main['price']}\n"
                    f"RSI: {main['rsi']}\n"
                    f"SL: {main['stop_loss']} TP: {main['take_profit']}\n"
                    f"RR: {main['risk_reward']}\n"
                    f"{final_strength}"
                )
                req.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg},
                    timeout=5
                )
            except:
                pass
    except Exception as e:
        import traceback
        return {"error": repr(e), "trace": traceback.format_exc()}


@app.post("/monitor/start")
async def start_monitor(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    market: str = "spot"
):
    try:
        from monitor.trade_monitor import OpenTrade, monitor_trade
        trade = OpenTrade(
            symbol=symbol, direction=direction,
            entry_price=entry_price, stop_loss=stop_loss,
            take_profit=take_profit, market=market
        )
        active_trades[symbol] = trade
        asyncio.create_task(monitor_trade(trade))
        return {"status": "Monitoring started ✅", "symbol": symbol}
    except Exception as e:
        return {"error": str(e)}

@app.get("/monitor/active")
def get_active_trades():
    return {
        symbol: {
            "direction":   t.direction,
            "entry_price": t.entry_price,
            "stop_loss":   t.stop_loss,
            "take_profit": t.take_profit,
            "opened_at":   str(t.opened_at)
        }
        for symbol, t in active_trades.items()
    }

@app.delete("/monitor/stop/{symbol}")
def stop_monitor(symbol: str):
    if symbol in active_trades:
        del active_trades[symbol]
        return {"status": f"Stopped monitoring {symbol} ✅"}
    return {"status": "Trade not found"}