import asyncio
from fastapi import FastAPI

app = FastAPI(title="MEXC Trading Bot", version="2.0.0")

# ── lazy-load clients ──────────────────────────────────
def get_spot():
    from mexc.client import MEXCSpotClient
    return MEXCSpotClient()

def get_futures():
    from mexc.client import MEXCFuturesClient
    return MEXCFuturesClient()

active_trades: dict = {}

@app.get("/")
def root():
    return {"status": "Bot is running ✅", "version": "2.0.0 - Multi Timeframe"}

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
        spot = get_spot()
        return spot.get_ticker(symbol)
    except Exception as e:
        return {"error": str(e)}

@app.get("/account/spot")
def get_spot_account():
    try:
        spot = get_spot()
        return spot.get_account()
    except Exception as e:
        return {"error": str(e)}

@app.get("/account/futures")
def get_futures_account():
    try:
        futures = get_futures()
        return futures.get_account_assets()
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════
# MULTI TIMEFRAME SIGNAL — checks 15m + 1h + 4h
# ══════════════════════════════════════════════════════
@app.get("/signal/{symbol}")
def get_signal(symbol: str = "BTCUSDT"):
    try:
        from strategy.indicators import (
            prepare_dataframe, add_indicators, generate_signal
        )
        spot = get_spot()

        # ── Fetch all 3 timeframes ─────────────────────
        klines_15m = spot.get_klines(symbol, interval="15m", limit=100)
        klines_1h  = spot.get_klines(symbol, interval="1h",  limit=100)
        klines_4h  = spot.get_klines(symbol, interval="4h",  limit=100)

        # ── Generate signal for each timeframe ─────────
        df_15m = add_indicators(prepare_dataframe(klines_15m))
        df_1h  = add_indicators(prepare_dataframe(klines_1h))
        df_4h  = add_indicators(prepare_dataframe(klines_4h))

        sig_15m = generate_signal(df_15m)
        sig_1h  = generate_signal(df_1h)
        sig_4h  = generate_signal(df_4h)

        # ── Multi Timeframe Agreement Logic ────────────
        signals = [sig_15m["signal"], sig_1h["signal"], sig_4h["signal"]]
        buy_count  = signals.count("BUY")
        sell_count = signals.count("SELL")

        # All 3 agree = STRONG signal
        if buy_count == 3:
            final_signal = "BUY"
            agreement    = "🔥 STRONG BUY — All 3 timeframes agree!"
            mtf_confidence = "HIGH"
        elif sell_count == 3:
            final_signal = "SELL"
            agreement    = "🔥 STRONG SELL — All 3 timeframes agree!"
            mtf_confidence = "HIGH"
        # 2 out of 3 agree = MODERATE signal
        elif buy_count == 2:
            final_signal = "BUY"
            agreement    = "✅ BUY — 2 of 3 timeframes agree"
            mtf_confidence = "MEDIUM"
        elif sell_count == 2:
            final_signal = "SELL"
            agreement    = "✅ SELL — 2 of 3 timeframes agree"
            mtf_confidence = "MEDIUM"
        # No agreement = HOLD
        else:
            final_signal = "HOLD"
            agreement    = "⏳ HOLD — Timeframes disagree"
            mtf_confidence = "LOW"

        # ── Use the 1h signal as the main data source ──
        main = sig_1h

        return {
            "symbol":          symbol,
            "signal":          final_signal,
            "mtf_confidence":  mtf_confidence,
            "agreement":       agreement,
            "timeframes": {
                "15m": sig_15m["signal"],
                "1h":  sig_1h["signal"],
                "4h":  sig_4h["signal"],
            },
            "confidence":    main["confidence"],
            "price":         main["price"],
            "rsi":           main["rsi"],
            "macd_hist":     main["macd_hist"],
            "ema9":          main["ema9"],
            "ema21":         main["ema21"],
            "ema50":         main["ema50"],
            "bb_lower":      main["bb_lower"],
            "bb_upper":      main["bb_upper"],
            "atr":           main["atr"],
            "adx":           main["adx"],
            "high_volume":   main["high_volume"],
            "strong_trend":  main["strong_trend"],
            "stop_loss":     main["stop_loss"],
            "take_profit":   main["take_profit"],
            "position_size": main["position_size"],
            "risk_reward":   main["risk_reward"],
            "reasons":       [agreement] + main["reasons"],
        }

    except Exception as e:
        return {"error": str(e)}


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
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            market=market
        )
        active_trades[symbol] = trade
        asyncio.create_task(monitor_trade(trade))
        return {
            "status":      "Monitoring started ✅",
            "symbol":      symbol,
            "direction":   direction,
            "entry":       entry_price,
            "stop_loss":   stop_loss,
            "take_profit": take_profit
        }
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
