import asyncio
import os
import time
from fastapi import FastAPI

app = FastAPI(title="MEXC Trading Bot", version="5.0.0")

def get_spot():
    from mexc.client import MEXCSpotClient
    return MEXCSpotClient()

active_trades: dict = {}

# ── Signal cooldown state (Fix 3: stop signal flipping) ──
# Stores: { symbol: { "signal": "BUY", "confirmed_at": timestamp, "count": 3 } }
_signal_state: dict = {}
CONFIRM_COUNT   = 3      # signal must appear 3 times in a row to confirm
COOLDOWN_SECS   = 900    # once confirmed, hold for 15 minutes minimum

# ── Last Telegram alert tracker (avoid spam) ──
_last_alert: dict = {}   # { symbol: { "signal": "BUY", "sent_at": timestamp } }
ALERT_COOLDOWN  = 900    # don't resend same signal for 15 minutes


def send_telegram(msg: str):
    """Send a Telegram message."""
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


def maybe_send_alert(symbol: str, signal: str, price: float,
                     sl: float, tp: float, rr: str, strength: str):
    """Send Telegram only if signal changed or cooldown expired."""
    now  = time.time()
    last = _last_alert.get(symbol, {})

    same_signal   = last.get("signal") == signal
    within_window = (now - last.get("sent_at", 0)) < ALERT_COOLDOWN

    if same_signal and within_window:
        return  # already alerted recently for this signal

    _last_alert[symbol] = {"signal": signal, "sent_at": now}
    send_telegram(
        f"SIGNAL {signal} - {symbol}\n"
        f"Price: {price}\n"
        f"SL: {sl}  TP: {tp}\n"
        f"RR: {rr}\n"
        f"{strength}"
    )


def stabilize_signal(symbol: str, raw_signal: str) -> str:
    """
    Fix 3 — Signal stability filter.
    Only confirm a signal after it appears CONFIRM_COUNT times in a row.
    Once confirmed hold it for COOLDOWN_SECS even if it flips.
    """
    now   = time.time()
    state = _signal_state.get(symbol, {"signal": "HOLD", "confirmed_at": 0, "count": 0, "candidate": "HOLD"})

    # Still within cooldown of confirmed signal — don't change
    if state["signal"] != "HOLD" and (now - state["confirmed_at"]) < COOLDOWN_SECS:
        return state["signal"]

    # Count consecutive same raw signals
    if raw_signal == state.get("candidate"):
        state["count"] += 1
    else:
        state["candidate"] = raw_signal
        state["count"]     = 1

    # Confirm only after CONFIRM_COUNT consecutive
    if state["count"] >= CONFIRM_COUNT:
        state["signal"]       = raw_signal
        state["confirmed_at"] = now
        state["count"]        = 0

    _signal_state[symbol] = state
    return state["signal"]


def compute_signal(symbol: str) -> dict:
    """
    Core signal computation — used by both /signal endpoint and background scanner.
    Fix 1: SL/TP now always computed from final_signal, not just 1h.
    """
    from strategy.indicators import prepare_dataframe, add_indicators, generate_signal
    from strategy.orderbook  import analyze_orderbook

    spot = get_spot()

    klines_15m = spot.get_klines(symbol, interval="15m", limit=100)
    klines_1h  = spot.get_klines(symbol, interval="1h",  limit=100)
    klines_4h  = spot.get_klines(symbol, interval="4h",  limit=100)

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
        raw_signal    = raw_signal
        final_strength = f"🔥 STRONG {raw_signal} — Indicators + Order Book confirm!"
    elif raw_signal in ("BUY", "SELL"):
        final_strength = f"✅ {raw_signal} — Indicators agree"
    else:
        final_strength = "⏳ HOLD — No clear direction"

    # Fix 3: stabilize before using
    final_signal = stabilize_signal(symbol, raw_signal)
    if final_signal != raw_signal:
        final_strength = f"⏳ HOLD — Waiting for signal to confirm ({raw_signal} pending)"

    # Fix 1: SL/TP from final_signal direction, using 1h ATR for levels
    main    = sig_1h
    price   = main["price"]
    atr_val = main["atr"]

    if final_signal == "BUY":
        stop_loss   = round(price - (atr_val * 1.5), 2)   # BELOW entry ✅
        take_profit = round(price + (atr_val * 3.0), 2)   # ABOVE entry ✅
    elif final_signal == "SELL":
        stop_loss   = round(price + (atr_val * 1.5), 2)   # ABOVE entry ✅
        take_profit = round(price - (atr_val * 3.0), 2)   # BELOW entry ✅
    else:
        stop_loss   = None
        take_profit = None

    if stop_loss is not None and take_profit is not None:
        risk        = abs(price - stop_loss)
        reward      = abs(take_profit - price)
        risk_reward = f"1:{round(reward/risk, 1)}" if risk > 0 else "1:2"
    else:
        risk_reward = "N/A"

    all_reasons = [final_strength, mtf_agreement] + ob_analysis["ob_reasons"] + main["reasons"]

    # Fix 2: Telegram alert with dedup
    if final_signal in ("BUY", "SELL") and stop_loss and take_profit:
        maybe_send_alert(symbol, final_signal, price, stop_loss, take_profit, risk_reward, final_strength)

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
        "price":             price,
        "rsi":               main["rsi"],
        "macd_hist":         main["macd_hist"],
        "ema9":              main["ema9"],
        "ema21":             main["ema21"],
        "ema50":             main["ema50"],
        "bb_lower":          main["bb_lower"],
        "bb_upper":          main["bb_upper"],
        "atr":               main["atr"],
        "adx":               main["adx"],
        "stop_loss":         stop_loss,
        "take_profit":       take_profit,
        "position_size":     main["position_size"],
        "risk_reward":       risk_reward,
        "score":             main.get("score"),
        "vwap":              main.get("vwap"),
        "vol_ratio":         main.get("vol_ratio"),
        "stoch_k":           main.get("stoch_k"),
        "reasons":           all_reasons,
    }


# ── Fix 2: Background scanner — runs every 5 minutes automatically ──
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

async def background_scanner():
    """Scans all pairs every 5 minutes. Sends Telegram automatically."""
    await asyncio.sleep(10)  # wait for server to start
    while True:
        for symbol in PAIRS:
            try:
                compute_signal(symbol)   # Telegram sent inside if signal valid
            except Exception as e:
                print(f"[Scanner] {symbol} error: {e}")
            await asyncio.sleep(3)       # small gap between pairs
        await asyncio.sleep(300)         # wait 5 minutes before next full scan


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_scanner())


# ── Endpoints ──────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "Bot is running ✅", "version": "5.0.0"}

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
                "price":      sig["price"]
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

@app.post("/monitor/start")
async def start_monitor(
    symbol: str, direction: str, entry_price: float,
    stop_loss: float, take_profit: float, market: str = "spot"
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

from strategy.auto_trader import auto_trader

@app.get("/auto/scan")
def auto_scan():
    try:
        auto_trader.scan_and_trade()
        return {
            "status":      "Scan complete",
            "open_trades": len(auto_trader.open_trades),
            "trades":      auto_trader.open_trades,
            "history":     auto_trader.trade_history[-5:]
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/auto/status")
def auto_status():
    return {
        "open_trades":  auto_trader.open_trades,
        "total_closed": len(auto_trader.trade_history),
        "history":      auto_trader.trade_history[-10:]
    }
