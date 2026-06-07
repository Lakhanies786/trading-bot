import asyncio
import os
import time
from fastapi import FastAPI, HTTPException

app = FastAPI(title="MEXC Trading Bot", version="6.1.0")

def get_spot():
    from mexc.client import MEXCSpotClient
    return MEXCSpotClient()

# ✅ FIX: Separate dict for CONFIRMED trades only (user must explicitly start monitor)
active_trades: dict = {}

_signal_state: dict = {}
CONFIRM_COUNT  = 3
COOLDOWN_SECS  = 900

_last_alert: dict = {}
ALERT_COOLDOWN = 900


def send_telegram(msg: str):
    try:
        import requests as req
        token   = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=5
        )
    except:
        pass


def maybe_send_alert(symbol, signal, price, sl, tp, rr, strength):
    now  = time.time()
    last = _last_alert.get(symbol, {})
    if last.get("signal") == signal and (now - last.get("sent_at", 0)) < ALERT_COOLDOWN:
        return
    _last_alert[symbol] = {"signal": signal, "sent_at": now}
    send_telegram(
        f"SIGNAL {signal} - {symbol}\n"
        f"Price: {price}\nSL: {sl}  TP: {tp}\nRR: {rr}\n{strength}"
    )


def stabilize_signal(symbol: str, raw_signal: str) -> str:
    now   = time.time()
    state = _signal_state.get(symbol, {
        "signal": "HOLD", "confirmed_at": 0, "count": 0, "candidate": "HOLD"
    })
    if state["signal"] != "HOLD" and (now - state["confirmed_at"]) < COOLDOWN_SECS:
        return state["signal"]
    if raw_signal == state.get("candidate"):
        state["count"] += 1
    else:
        state["candidate"] = raw_signal
        state["count"]     = 1
    if state["count"] >= CONFIRM_COUNT:
        state["signal"]       = raw_signal
        state["confirmed_at"] = now
        state["count"]        = 0
    _signal_state[symbol] = state
    return state["signal"]


def compute_signal(symbol: str) -> dict:
    from strategy.indicators import prepare_dataframe, add_indicators, generate_signal
    from strategy.orderbook  import analyze_orderbook

    spot = get_spot()

    klines_15m = spot.get_klines(symbol, interval="15m", limit=200)
    klines_1h  = spot.get_klines(symbol, interval="1h",  limit=200)
    klines_4h  = spot.get_klines(symbol, interval="4h",  limit=200)

    df_15m = add_indicators(prepare_dataframe(klines_15m))
    df_1h  = add_indicators(prepare_dataframe(klines_1h))
    df_4h  = add_indicators(prepare_dataframe(klines_4h))

    sig_15m = generate_signal(df_15m)
    sig_1h  = generate_signal(df_1h)
    sig_4h  = generate_signal(df_4h)

    orderbook   = spot.get_orderbook(symbol, limit=50)
    ob_analysis = analyze_orderbook(orderbook, sig_1h["price"])
    ob_signal   = ob_analysis["ob_signal"]

    signals    = [sig_15m["signal"], sig_1h["signal"], sig_4h["signal"]]
    buy_count  = signals.count("BUY")
    sell_count = signals.count("SELL")

    if buy_count >= 2:
        raw_signal     = "BUY"
        mtf_confidence = "HIGH" if buy_count == 3 else "MEDIUM"
        mtf_agreement  = f"✅ {buy_count}/3 timeframes say BUY"
    elif sell_count >= 2:
        raw_signal     = "SELL"
        mtf_confidence = "HIGH" if sell_count == 3 else "MEDIUM"
        mtf_agreement  = f"✅ {sell_count}/3 timeframes say SELL"
    else:
        raw_signal     = "HOLD"
        mtf_confidence = "LOW"
        mtf_agreement  = "⏳ Timeframes disagree — wait"

    if raw_signal == ob_signal and raw_signal != "HOLD":
        final_strength = f"🔥 STRONG {raw_signal} — Indicators + Order Book confirm!"
    elif raw_signal in ("BUY", "SELL"):
        final_strength = f"✅ {raw_signal} — Indicators agree"
    else:
        final_strength = "⏳ HOLD — No clear direction"

    final_signal = stabilize_signal(symbol, raw_signal)
    if final_signal != raw_signal:
        final_strength = f"⏳ HOLD — Waiting to confirm ({raw_signal} pending)"

    main    = sig_1h
    price   = main["price"]
    atr_val = main["atr"]

    if final_signal == "BUY":
        stop_loss   = round(price - (atr_val * 1.5), 4)
        take_profit = round(price + (atr_val * 3.0), 4)
    elif final_signal == "SELL":
        stop_loss   = round(price + (atr_val * 1.5), 4)
        take_profit = round(price - (atr_val * 3.0), 4)
    else:
        stop_loss   = None
        take_profit = None

    if stop_loss and take_profit:
        risk        = abs(price - stop_loss)
        reward      = abs(take_profit - price)
        risk_reward = f"1:{round(reward/risk, 1)}" if risk > 0 else "1:2"
    else:
        risk_reward = "N/A"

    all_reasons = [final_strength, mtf_agreement] + ob_analysis["ob_reasons"] + main["reasons"]

    if final_signal in ("BUY", "SELL") and stop_loss and take_profit:
        maybe_send_alert(symbol, final_signal, price, stop_loss, take_profit, risk_reward, final_strength)

    return {
        "symbol":             symbol,
        "signal":             final_signal,
        "confidence":         main.get("confidence", "0%"),
        "mtf_confidence":     mtf_confidence,
        "agreement":          final_strength,
        "timeframes": {
            "15m": sig_15m["signal"],
            "1h":  sig_1h["signal"],
            "4h":  sig_4h["signal"],
        },
        "ob_signal":          ob_signal,
        "bid_ask_ratio":      ob_analysis["bid_ask_ratio"],
        "ob_imbalance":       ob_analysis.get("imbalance", 0),
        "ob_liquidity":       ob_analysis.get("liquidity", 0),
        "ob_spread_pct":      ob_analysis.get("spread_pct", 0),
        "total_bid_volume":   ob_analysis["total_bid_volume"],
        "total_ask_volume":   ob_analysis["total_ask_volume"],
        "biggest_buy_wall":   ob_analysis["biggest_buy_wall"],
        "biggest_sell_wall":  ob_analysis["biggest_sell_wall"],
        "buy_wall_price":     ob_analysis["buy_wall_price"],
        "sell_wall_price":    ob_analysis["sell_wall_price"],
        "price":              price,
        "rsi":                main["rsi"],
        "macd_hist":          main["macd_hist"],
        "ema9":               main["ema9"],
        "ema21":              main["ema21"],
        "ema50":              main["ema50"],
        "adx":                main["adx"],
        "adx_pos":            main.get("adx_pos", 0),
        "adx_neg":            main.get("adx_neg", 0),
        "cvd":                main.get("cvd", 0),
        "roc":                main.get("roc", 0),
        "bb_lower":           main["bb_lower"],
        "bb_upper":           main["bb_upper"],
        "atr":                main["atr"],
        "stop_loss":          stop_loss,
        "take_profit":        take_profit,
        "position_size":      main["position_size"],
        "risk_reward":        risk_reward,
        "score":              main.get("score"),
        "vwap":               main.get("vwap"),
        "vol_ratio":          main.get("vol_ratio"),
        "stoch_k":            main.get("stoch_k"),
        "nearest_support":    main.get("nearest_support"),
        "nearest_resistance": main.get("nearest_resistance"),
        "fib_618":            main.get("fib_618"),
        "fib_382":            main.get("fib_382"),
        "reasons":            all_reasons,
    }


# ── Background scanner — SIGNAL ONLY, never starts monitor ────────────
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

async def background_scanner():
    await asyncio.sleep(10)
    while True:
        for symbol in PAIRS:
            try:
                # ✅ FIX: scanner only computes signals, NEVER touches active_trades
                compute_signal(symbol)
            except Exception as e:
                print(f"[Scanner] {symbol} error: {e}")
            await asyncio.sleep(3)
        await asyncio.sleep(300)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_scanner())


# ── Endpoints ─────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "Bot is running ✅", "version": "6.1.0"}

@app.get("/health")
def health():
    try:
        price = get_spot().get_ticker("BTCUSDT")
        return {"status": "ok ✅", "btc_price": price.get("price")}
    except Exception as e:
        return {"error": str(e)}

@app.get("/price/{symbol}")
def get_price(symbol: str):
    try:
        return get_spot().get_ticker(symbol)
    except Exception as e:
        return {"error": str(e)}

@app.get("/signal/{symbol}")
def get_signal(symbol: str = "BTCUSDT"):
    try:
        return compute_signal(symbol)
    except Exception as e:
        import traceback
        return {"error": repr(e), "trace": traceback.format_exc()}

@app.get("/scan/all")
def scan_all_pairs():
    results = []
    for symbol in PAIRS:
        try:
            sig = compute_signal(symbol)
            results.append({
                "symbol":     symbol,
                "signal":     sig["signal"],
                "confidence": sig["confidence"],
                "score":      sig["score"],
                "rsi":        sig["rsi"],
                "price":      sig["price"],
                "adx":        sig["adx"],
                "cvd":        sig.get("cvd", 0),
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})
    return {"pairs": results}

@app.get("/account/spot")
def get_spot_account():
    try:
        return get_spot().get_account()
    except Exception as e:
        return {"error": str(e)}


# ✅ FIX: /monitor/start now validates everything before starting
@app.post("/monitor/start")
async def start_monitor(
    symbol: str, direction: str, entry_price: float,
    stop_loss: float, take_profit: float, market: str = "spot"
):
    try:
        # ✅ FIX 1: Reject if trade already being monitored
        if symbol in active_trades:
            raise HTTPException(
                status_code=400,
                detail=f"{symbol} is already being monitored. Stop it first."
            )

        # ✅ FIX 2: Validate all values are real before creating trade
        if entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
            raise HTTPException(
                status_code=400,
                detail="entry_price, stop_loss and take_profit must all be greater than 0"
            )

        # ✅ FIX 3: Validate SL is on correct side of entry
        if direction == "BUY" and stop_loss >= entry_price:
            raise HTTPException(
                status_code=400,
                detail=f"BUY trade: stop_loss ({stop_loss}) must be BELOW entry ({entry_price})"
            )
        if direction == "SELL" and stop_loss <= entry_price:
            raise HTTPException(
                status_code=400,
                detail=f"SELL trade: stop_loss ({stop_loss}) must be ABOVE entry ({entry_price})"
            )

        # ✅ FIX 4: Validate direction
        if direction not in ("BUY", "SELL"):
            raise HTTPException(
                status_code=400,
                detail="direction must be BUY or SELL"
            )

        from monitor.trade_monitor import OpenTrade, monitor_trade
        trade = OpenTrade(
            symbol=symbol, direction=direction,
            entry_price=entry_price, stop_loss=stop_loss,
            take_profit=take_profit, market=market
        )
        active_trades[symbol] = trade
        asyncio.create_task(monitor_trade(trade))
        return {
            "status":      "Monitoring started ✅",
            "symbol":      symbol,
            "direction":   direction,
            "entry_price": entry_price,
            "stop_loss":   stop_loss,
            "take_profit": take_profit,
        }
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}


@app.get("/monitor/active")
def get_active_trades():
    # ✅ FIX: Returns empty dict if no trades — never shows phantom trades
    if not active_trades:
        return {"message": "No active trades being monitored", "trades": {}}
    return {
        "trades": {
            symbol: {
                "direction":   t.direction,
                "entry_price": t.entry_price,
                "stop_loss":   t.stop_loss,
                "take_profit": t.take_profit,
                "trailing_sl": t.trailing_sl,
                "opened_at":   str(t.opened_at)
            }
            for symbol, t in active_trades.items()
        }
    }


@app.delete("/monitor/stop/{symbol}")
def stop_monitor(symbol: str):
    if symbol in active_trades:
        del active_trades[symbol]
        return {"status": f"Stopped monitoring {symbol} ✅"}
    return {"status": "Trade not found — nothing to stop"}


from strategy.auto_trader import auto_trader

@app.get("/auto/scan")
def auto_scan():
    try:
        auto_trader.scan_and_trade()
        return {
            "status":      "Scan complete",
            "open_trades": len(auto_trader.open_trades),
            "trades":      auto_trader.open_trades,
            "history":     auto_trader.trade_history[-5:],
            "daily_pnl":   auto_trader.daily_pnl,
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/auto/status")
def auto_status():
    return {
        "open_trades":        auto_trader.open_trades,
        "total_closed":       len(auto_trader.trade_history),
        "history":            auto_trader.trade_history[-10:],
        "daily_pnl":          auto_trader.daily_pnl,
        "consecutive_losses": auto_trader.consecutive_losses,
    }

@app.get("/performance")
def performance():
    history = auto_trader.trade_history
    if not history:
        return {"message": "No trades yet"}
    wins      = [t for t in history if t["pnl_pct"] > 0]
    losses    = [t for t in history if t["pnl_pct"] <= 0]
    total_pnl = sum(t["pnl_pct"] for t in history)
    win_rate  = round(len(wins) / len(history) * 100, 1) if history else 0
    avg_win   = round(sum(t["pnl_pct"] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss  = round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0
    return {
        "total_trades":  len(history),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      f"{win_rate}%",
        "total_pnl":     f"{round(total_pnl, 2)}%",
        "avg_win":       f"{avg_win}%",
        "avg_loss":      f"{avg_loss}%",
        "daily_pnl":     f"{auto_trader.daily_pnl:.2f}%",
        "recent_trades": history[-5:],
    }
