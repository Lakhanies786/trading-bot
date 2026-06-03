import asyncio
from fastapi import FastAPI

app = FastAPI(title="MEXC Trading Bot", version="1.0.0")

# ── lazy-load clients so imports resolve correctly ──
def get_spot():
    from mexc.client import MEXCSpotClient
    return MEXCSpotClient()

def get_futures():
    from mexc.client import MEXCFuturesClient
    return MEXCFuturesClient()

active_trades: dict = {}

@app.get("/")
def root():
    return {"status": "Bot is running ✅", "mode": "DEMO"}

@app.get("/health")
def health():
    try:
        spot = get_spot()
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

@app.get("/signal/{symbol}")
def get_signal(symbol: str = "BTCUSDT"):
    try:
        from strategy.indicators import (
            prepare_dataframe, add_indicators, generate_signal
        )
        spot   = get_spot()
        klines = spot.get_klines(symbol, interval="15m")
        df     = prepare_dataframe(klines)
        df     = add_indicators(df)
        signal = generate_signal(df)
        return {"symbol": symbol, **signal}
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
            "status":     "Monitoring started ✅",
            "symbol":     symbol,
            "direction":  direction,
            "entry":      entry_price,
            "stop_loss":  stop_loss,
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
    