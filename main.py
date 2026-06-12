import asyncio
import io
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI(title="MEXC Trading Bot", version="8.0.0")

# ── Log file paths — always relative to THIS file, never CWD ──────────
# This prevents logs from resetting when server is restarted from a
# different working directory. Logs stay in the same folder as main.py.
_BASE_DIR = Path(__file__).parent.resolve()

SIGNAL_LOG_FILE      = str(_BASE_DIR / "signal_log.json")
BLOCKED_LOG_FILE     = str(_BASE_DIR / "blocked_signals.json")
SCALP_LOG_FILE       = str(_BASE_DIR / "scalp_signal_log.json")
SCALP_BLOCKED_FILE   = str(_BASE_DIR / "scalp_blocked_log.json")
MIN_CONFIDENCE   = 50      # Lowered from 70 — testing phase (was filtering too aggressively)
MIN_SCORE        = 10      # Lowered from 11 — testing phase
MIN_VOL_RATIO    = 0.8     # Lowered from 1.0 — testing phase
ADX_MIN          = 20      # Lowered from 25 — most ETH/BTC signals were 20-24 and got rejected

# ── Scalping Filter Constants (stricter — higher accuracy needed) ──────
SCALP_MIN_CONFIDENCE = 55   # lowered from 65 — testing phase (collecting data)
SCALP_MIN_SCORE      = 10   # lowered from 12 — testing phase
SCALP_MIN_VOL_RATIO  = 0.6  # lowered from 1.2 — scalp signals have naturally lower volume on 5m
SCALP_ADX_MIN        = 22   # trend must be clear
TRADE_HRS_UTC    = (8, 17) # UTC trading hours
REQUIRE_4H       = True
MAX_SIGNAL_AGE   = 24
REQUIRE_ALL_3TF  = True
MIN_RSI_BUY      = 45
MAX_RSI_BUY      = 68
MIN_RSI_SELL     = 32
MAX_RSI_SELL     = 55
NEWS_BLOCK_ENABLED = True  # NEW — set False to disable news filtering

# ── Trade Journal Grade Thresholds ────────────────────────────────────
# A-Grade: Current strict rules — the "real" signals
GRADE_A = {"min_confidence": 70, "min_score": 11, "min_adx": 25, "min_volume": 1.0, "min_tf": 3}
# B-Grade: Relaxed — signals worth tracking but not trading live yet
GRADE_B = {"min_confidence": 55, "min_score":  9, "min_adx": 20, "min_volume": 0.8, "min_tf": 2}
# C-Grade: Everything else that has a directional signal (not HOLD)

def load_signal_log() -> list:
    if not Path(SIGNAL_LOG_FILE).exists():
        return []
    try:
        with open(SIGNAL_LOG_FILE) as f:
            return json.load(f)
    except:
        return []

def save_signal_log(signals: list):
    with open(SIGNAL_LOG_FILE, "w") as f:
        json.dump(signals, f, indent=2)

def load_blocked_log() -> list:
    if not Path(BLOCKED_LOG_FILE).exists():
        return []
    try:
        with open(BLOCKED_LOG_FILE) as f:
            return json.load(f)
    except:
        return []

def save_blocked_log(blocked: list):
    with open(BLOCKED_LOG_FILE, "w") as f:
        json.dump(blocked, f, indent=2)

blocked_log: list = load_blocked_log()

# ── Scalp logs ────────────────────────────────────────────────────────
def load_scalp_log() -> list:
    if not Path(SCALP_LOG_FILE).exists(): return []
    try:
        with open(SCALP_LOG_FILE) as f: return json.load(f)
    except: return []

def save_scalp_log(signals: list):
    with open(SCALP_LOG_FILE, "w") as f: json.dump(signals, f, indent=2)

def load_scalp_blocked() -> list:
    if not Path(SCALP_BLOCKED_FILE).exists(): return []
    try:
        with open(SCALP_BLOCKED_FILE) as f: return json.load(f)
    except: return []

def save_scalp_blocked(blocked: list):
    with open(SCALP_BLOCKED_FILE, "w") as f: json.dump(blocked, f, indent=2)

scalp_log:     list = load_scalp_log()
scalp_blocked: list = load_scalp_blocked()

signal_log: list = load_signal_log()


def get_spot():
    from mexc.client import MEXCSpotClient
    return MEXCSpotClient()

active_trades: dict = {}
_signal_state: dict = {}
CONFIRM_COUNT  = 2
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


def quality_gate_score(sig: dict) -> tuple[int, list[str], list[str]]:
    """
    Returns (passed_count, passed_list, failed_list).
    HARD BLOCKS (instant fail regardless of other checks):
      - Volume < 1.0x
      - Not all 3 timeframes agree
      - MACD opposes signal
    SCORED CHECKS (must pass 6/7 for GREEN, 5/7 for blocked):
    """
    signal  = sig.get("signal", "HOLD")
    conf    = int(sig.get("confidence", "0%").replace("%", ""))
    score   = int(sig.get("score", "0/16").split("/")[0])
    macd    = sig.get("macd_hist") or 0
    vol     = sig.get("vol_ratio") or 0
    adx     = sig.get("adx") or 0
    rsi     = sig.get("rsi") or 50
    mtf     = sig.get("mtf_confidence", "LOW")
    tf      = sig.get("timeframes", {})

    tf_vals   = [tf.get("15m"), tf.get("1h"), tf.get("4h")]
    all_3_tf  = tf_vals.count(signal) == 3          # ALL 3 must agree now
    macd_ok   = (signal == "BUY" and macd > 0) or (signal == "SELL" and macd < 0)
    vol_ok    = vol >= MIN_VOL_RATIO                 # uses constant — change MIN_VOL_RATIO at top
    rsi_ok    = (
        (signal == "BUY"  and MIN_RSI_BUY  <= rsi <= MAX_RSI_BUY)  or
        (signal == "SELL" and MIN_RSI_SELL <= rsi <= MAX_RSI_SELL)
    )

    checks = [
        (conf   >= MIN_CONFIDENCE, f"Confidence {conf}%",         f"Confidence {conf}% (need {MIN_CONFIDENCE}%+)"),
        (score  >= MIN_SCORE,      f"Score {score}/16",            f"Score {score}/16 (need {MIN_SCORE}+)"),
        (macd_ok,                  f"MACD confirms {signal}",      f"MACD opposes {signal} 🚫"),
        (vol_ok,                   f"Volume {vol:.1f}x",           f"Volume {vol:.1f}x (need {MIN_VOL_RATIO}x+) 🚫"),
        (all_3_tf,                 f"All 3 timeframes agree ✓",    f"Only {tf_vals.count(signal)}/3 TF agree 🚫"),
        (adx    >= ADX_MIN,        f"ADX {adx:.1f} strong trend",  f"ADX {adx:.1f} (need {ADX_MIN}+)"),
        (rsi_ok,                   f"RSI {rsi:.1f} ideal zone",    f"RSI {rsi:.1f} — {'overbought' if rsi > MAX_RSI_BUY else 'no momentum'}"),
    ]

    passed = [label for ok, label, _ in checks if ok]
    failed = [bad   for ok, _,   bad in checks if not ok]
    return len(passed), passed, failed


def maybe_send_alert(symbol, signal, price, sl, tp, rr, strength, sig: dict):
    """Only sends Telegram alert if signal passes quality gate (6+ / 7 checks)."""
    passed, passed_checks, failed_checks = quality_gate_score(sig)

    # Require HIGH quality (6+/7) for Telegram alert
    if passed < 6:
        return

    now  = time.time()
    last = _last_alert.get(symbol, {})
    if last.get("signal") == signal and (now - last.get("sent_at", 0)) < ALERT_COOLDOWN:
        return
    _last_alert[symbol] = {"signal": signal, "sent_at": now}

    gate_emoji = "🟢" if passed == 7 else "🟡"
    passed_str = "\n".join(f"  ✅ {c}" for c in passed_checks)
    failed_str = ("\n".join(f"  ❌ {c}" for c in failed_checks)) if failed_checks else "  —"
    news_sentiment = sig.get("news_sentiment", "UNKNOWN")
    news_score     = sig.get("news_score", 0)
    news_emoji     = "📰🟢" if news_sentiment == "BULLISH" else ("📰🔴" if news_sentiment == "BEARISH" else "📰⚪")

    send_telegram(
        f"{gate_emoji} HIGH QUALITY {signal} — {symbol}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Price: {price}\n"
        f"🛑 SL: {sl}    🎯 TP: {tp}\n"
        f"⚖️ R:R: {rr}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Quality Gate: {passed}/7 passed\n"
        f"{passed_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{news_emoji} News: {news_sentiment} (score {news_score})\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{strength}"
    )


def _maybe_send_compression_alert(symbol: str, bo: dict, price: float):
    """Sends a Phase 1 'watch this level' Telegram warning — 2h cooldown."""
    now  = time.time()
    key  = f"{symbol}_compression"
    last = _last_alert.get(key, {})
    if (now - last.get("sent_at", 0)) < 7200:
        return
    _last_alert[key] = {"sent_at": now}

    direction_emoji = "📈" if bo["direction"] == "UP" else "📉"
    target_str = f"${bo['measured_target']:,.4f}" if bo.get("measured_target") else "TBD"
    send_telegram(
        f"⚡ COMPRESSION ALERT — {symbol}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Current Price: ${price:,.4f}\n"
        f"{direction_emoji} Direction: {bo['direction']}\n"
        f"🎯 Watch Level: ${bo.get('watch_level', 0):,.4f}\n"
        f"📐 Measured Target: {target_str} (+{bo.get('move_pct', 0):.1f}%)\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Strength: {bo['strength']}\n"
        f"BB Width: {bo.get('bb_width', 0):.4f} (squeezed)\n"
        f"Volume: {bo.get('vol_ratio', 0):.1f}x (quiet = accumulation)\n"
        f"ADX: {bo.get('adx', 0):.1f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏳ NOT a trade yet — watch for candle CLOSE\n"
        f"beyond watch level with 2x+ volume to confirm."
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
    from strategy.indicators import detect_market_regime, detect_daily_bias, detect_breakout
    from strategy.orderbook  import analyze_orderbook

    spot = get_spot()

    klines_15m = spot.get_klines(symbol, interval="15m", limit=200)
    klines_1h  = spot.get_klines(symbol, interval="1h",  limit=200)
    klines_4h  = spot.get_klines(symbol, interval="4h",  limit=200)
    klines_1d  = spot.get_klines(symbol, interval="1d",  limit=100)  # daily for HTF bias

    df_15m = add_indicators(prepare_dataframe(klines_15m))
    df_1h  = add_indicators(prepare_dataframe(klines_1h))
    df_4h  = add_indicators(prepare_dataframe(klines_4h))
    df_1d  =                prepare_dataframe(klines_1d)   # no indicators needed — bias func adds its own

    # ── Market Regime (based on 1h) ───────────────────────────────────
    regime_info = detect_market_regime(df_1h)

    # ── Daily HTF Bias ────────────────────────────────────────────────
    bias_info = detect_daily_bias(df_1d)

    # ── Breakout Detection (Phase 1 + Phase 2) ────────────────────────
    breakout_info = detect_breakout(df_1h, df_4h)

    # ── News Safety Check (Layer 0 — runs before signal logic) ────────
    from news_filter import check_news_safety
    news_info = check_news_safety(symbol) if NEWS_BLOCK_ENABLED else {
        "safe": True, "risk_level": "CLEAR",
        "reason": "News filter disabled",
        "sentiment": "UNKNOWN", "news_score": 0,
        "events": [], "risk_news": False,
    }

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

    # ── Market Regime Gate ────────────────────────────────────────────
    # Block signals in ranging or volatile markets
    regime_blocked = False
    if not regime_info["tradeable"] and final_signal != "HOLD":
        regime_blocked = True
        final_signal   = "HOLD"
        final_strength = f"🚫 BLOCKED — {regime_info['regime_reason']}"

    # ── Daily Bias Gate ───────────────────────────────────────────────
    # Only allow signals that align with the daily trend
    bias_blocked = False
    if not regime_blocked and final_signal != "HOLD":
        bias = bias_info["bias"]
        if bias == "BULLISH" and final_signal == "SELL":
            bias_blocked   = True
            final_signal   = "HOLD"
            final_strength = f"🚫 BLOCKED — Daily bias is BULLISH, no SELL signals allowed"
        elif bias == "BEARISH" and final_signal == "BUY":
            bias_blocked   = True
            final_signal   = "HOLD"
            final_strength = f"🚫 BLOCKED — Daily bias is BEARISH, no BUY signals allowed"

    # ── News Block Gate ───────────────────────────────────────────────
    # Block signal entirely if high-impact event or risk news detected
    news_blocked = False
    if not regime_blocked and not bias_blocked and not news_info["safe"]:
        news_blocked   = True
        final_signal   = "HOLD"
        final_strength = news_info["reason"]

    # ── Additional: block BUY if news sentiment strongly bearish ──────
    if (not news_blocked and not regime_blocked and not bias_blocked
            and final_signal == "BUY"
            and news_info["sentiment"] == "BEARISH"
            and news_info["news_score"] < -40):
        news_blocked   = True
        final_signal   = "HOLD"
        final_strength = f"🚫 BLOCKED — News sentiment strongly BEARISH (score {news_info['news_score']}) — no BUY allowed"

    # ── Phase 2 Breakout: promote to signal if confirmed ─────────────
    # If a confirmed breakout exists, treat it as a strong BUY/SELL
    # Does NOT override news blocks — too risky during high-impact events
    breakout_promotes = False
    if breakout_info["phase"] == 2:
        bo_dir = "BUY" if breakout_info["direction"] == "UP" else "SELL"
        # Only promote if NOT blocked by regime, bias, OR news
        if not regime_blocked and not bias_blocked and not news_blocked:
            if final_signal == "HOLD" or final_signal == bo_dir:
                final_signal    = bo_dir
                breakout_promotes = True
                final_strength  = breakout_info["message"]

    main    = sig_1h
    price   = main["price"]
    atr_val = main["atr"]

    # Use the actual directional signal for SL/TP calculation
    # even if final_signal was overridden to HOLD by news/regime/bias block
    sl_tp_signal = final_signal if final_signal != "HOLD" else main.get("signal", "HOLD")

    if sl_tp_signal == "BUY":
        stop_loss   = round(price - (atr_val * 2.5), 4)
        take_profit = round(price + (atr_val * 5.0), 4)
    elif sl_tp_signal == "SELL":
        stop_loss   = round(price + (atr_val * 2.5), 4)
        take_profit = round(price - (atr_val * 5.0), 4)
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

    # Build result dict first so we can pass it to quality gate
    result = {
        "symbol":             symbol,
        "signal":             final_signal,
        "pre_block_signal":   raw_signal,   # direction before ANY block (regime/bias/news)
        "news_blocked":       news_blocked,
        "regime_blocked":     regime_blocked,
        "bias_blocked":       bias_blocked,
        "confidence":         main.get("confidence", "0%"),
        "mtf_confidence":     mtf_confidence,
        "agreement":          final_strength,
        "timeframes": {
            "15m": sig_15m["signal"],
            "1h":  sig_1h["signal"],
            "4h":  sig_4h["signal"],
        },
        "ob_signal":          ob_signal,
        "ob_imbalance":       ob_analysis.get("imbalance", 0),
        "ob_liquidity":       ob_analysis.get("liquidity", 0),
        "ob_spread_pct":      ob_analysis.get("spread_pct", 0),
        "total_bid_volume":   ob_analysis["total_bid_volume"],
        "total_ask_volume":   ob_analysis["total_ask_volume"],
        "biggest_buy_wall":   ob_analysis["biggest_buy_wall"],
        "biggest_sell_wall":  ob_analysis["biggest_sell_wall"],
        "buy_wall_price":     ob_analysis["buy_wall_price"],
        "sell_wall_price":    ob_analysis["sell_wall_price"],
        # ── Market Regime ──────────────────────────────────
        "market_regime":       regime_info["regime"],
        "regime_reason":       regime_info["regime_reason"],
        "regime_tradeable":    regime_info["tradeable"],
        "atr_ratio":           regime_info["atr_ratio"],
        # ── Daily HTF Bias ─────────────────────────────────
        "daily_bias":          bias_info["bias"],
        "daily_bias_strength": bias_info["bias_strength"],
        "daily_bias_reason":   bias_info["bias_reason"],
        "ema21_daily":         bias_info["ema21_d"],
        "ema50_daily":         bias_info["ema50_d"],
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
        "fib_500":            main.get("fib_500"),
        "fib_382":            main.get("fib_382"),
        "reasons":            all_reasons,
        # ── Breakout Detection ──────────────────────────────
        "breakout_phase":     breakout_info["phase"],
        "breakout_status":    breakout_info["status"],
        "breakout_direction": breakout_info["direction"],
        "breakout_watch":     breakout_info.get("watch_level") or breakout_info.get("breakout_level"),
        "breakout_target":    breakout_info.get("measured_target"),
        "breakout_move_pct":  breakout_info.get("move_pct", 0),
        "breakout_strength":  breakout_info.get("strength", "NONE"),
        "breakout_message":   breakout_info["message"],
        "breakout_vol_ratio": breakout_info.get("vol_ratio", 0),
        # ── News Awareness ─────────────────────────────────
        "news_safe":          news_info["safe"],
        "news_risk_level":    news_info["risk_level"],
        "news_reason":        news_info["reason"],
        "news_sentiment":     news_info["sentiment"],
        "news_score":         news_info["news_score"],
        "news_risk":          news_info["risk_news"],
        "news_events":        [
            {"title": e["title"], "minutes_away": e["minutes_away"]}
            for e in news_info.get("events", [])[:3]
        ],
    }

    # ── Telegram: Phase 2 breakout fires through quality gate ─────────
    if final_signal in ("BUY", "SELL") and stop_loss and take_profit:
        maybe_send_alert(symbol, final_signal, price, stop_loss, take_profit,
                         risk_reward, final_strength, result)

    # ── Telegram: Phase 1 compression — separate "watch" alert ────────
    elif breakout_info["phase"] == 1 and breakout_info["strength"] in ("STRONG", "MODERATE"):
        _maybe_send_compression_alert(symbol, breakout_info, price)

    return result


# ── Signal log helpers ────────────────────────────────────────────────

def compute_scalp_signal(symbol: str) -> dict:
    """
    Scalping signal using 1m + 5m + 15m timeframes.
    Tighter SL/TP (ATR×1.0 / ATR×2.0) and stricter filters than swing.
    All 3 timeframes must agree for a valid scalp signal.
    """
    from strategy.indicators import prepare_dataframe, add_indicators, generate_signal
    from strategy.indicators import detect_market_regime, detect_daily_bias
    from strategy.orderbook  import analyze_orderbook

    spot = get_spot()

    klines_1m  = spot.get_klines(symbol, interval="1m",  limit=150)
    klines_5m  = spot.get_klines(symbol, interval="5m",  limit=150)
    klines_15m = spot.get_klines(symbol, interval="15m", limit=150)
    klines_1d  = spot.get_klines(symbol, interval="1d",  limit=50)

    df_1m  = add_indicators(prepare_dataframe(klines_1m))
    df_5m  = add_indicators(prepare_dataframe(klines_5m))
    df_15m = add_indicators(prepare_dataframe(klines_15m))
    df_1d  =                prepare_dataframe(klines_1d)

    sig_1m  = generate_signal(df_1m)
    sig_5m  = generate_signal(df_5m)
    sig_15m = generate_signal(df_15m)

    regime_info = detect_market_regime(df_5m)
    bias_info   = detect_daily_bias(df_1d)

    orderbook   = spot.get_orderbook(symbol, limit=50)
    ob_analysis = analyze_orderbook(orderbook, sig_5m["price"])
    ob_signal   = ob_analysis["ob_signal"]

    signals    = [sig_1m["signal"], sig_5m["signal"], sig_15m["signal"]]
    buy_count  = signals.count("BUY")
    sell_count = signals.count("SELL")

    if buy_count >= 2:
        raw_signal    = "BUY"
        mtf_agreement = f"✅ {buy_count}/3 scalp TF say BUY"
    elif sell_count >= 2:
        raw_signal    = "SELL"
        mtf_agreement = f"✅ {sell_count}/3 scalp TF say SELL"
    else:
        raw_signal    = "HOLD"
        mtf_agreement = "⏳ Scalp TF disagree — wait"

    # Stabilise scalp signal — require 2 consecutive agreements to fire
    # Uses separate state key so scalp doesn't interfere with swing state
    final_signal = stabilize_signal(f"SCALP_{symbol}", raw_signal)

    # ── Regime gate ───────────────────────────────────────────────────
    regime_blocked = False
    if not regime_info["tradeable"] and final_signal != "HOLD":
        regime_blocked = True
        final_signal   = "HOLD"

    # ── Daily Bias gate ───────────────────────────────────────────────
    bias_blocked = False
    if not regime_blocked and final_signal != "HOLD":
        bias = bias_info["bias"]
        if bias == "BULLISH" and final_signal == "SELL":
            bias_blocked = True
            final_signal = "HOLD"
        elif bias == "BEARISH" and final_signal == "BUY":
            bias_blocked = True
            final_signal = "HOLD"

    # ── News gate ─────────────────────────────────────────────────────
    news_blocked = False
    if NEWS_BLOCK_ENABLED and not regime_blocked and not bias_blocked:
        from news_filter import check_news_safety
        news_info = check_news_safety(symbol)
        if not news_info["safe"]:
            news_blocked = True
            final_signal = "HOLD"
    else:
        news_info = {"safe": True, "risk_level": "CLEAR", "reason": "",
                     "sentiment": "UNKNOWN", "news_score": 0, "events": []}

    # SL/TP — tighter than swing: ATR×1.0 stop, ATR×2.0 target (1:2 R:R)
    main    = sig_5m
    price   = main["price"]
    atr_val = main["atr"]
    sl_tp_signal = final_signal if final_signal != "HOLD" else raw_signal

    if sl_tp_signal == "BUY":
        stop_loss   = round(price - (atr_val * 1.0), 4)
        take_profit = round(price + (atr_val * 2.0), 4)
    elif sl_tp_signal == "SELL":
        stop_loss   = round(price + (atr_val * 1.0), 4)
        take_profit = round(price - (atr_val * 2.0), 4)
    else:
        stop_loss = take_profit = None

    risk_reward = "N/A"
    if stop_loss and take_profit:
        risk   = abs(price - stop_loss)
        reward = abs(take_profit - price)
        risk_reward = f"1:{round(reward/risk, 1)}" if risk > 0 else "1:2"

    result = {
        "mode":               "SCALP",
        "symbol":             symbol,
        "signal":             final_signal,
        "pre_block_signal":   raw_signal,
        "news_blocked":       news_blocked,
        "regime_blocked":     regime_blocked,
        "bias_blocked":       bias_blocked,
        "confidence":         main.get("confidence", "0%"),
        "score":              main.get("score", "0/16"),
        "adx":                round(main.get("adx") or 0, 2),
        "vol_ratio":          round(main.get("vol_ratio") or 0, 2),
        "rsi":                round(main.get("rsi") or 0, 2),
        "macd_hist":          round(main.get("macd_hist") or 0, 6),
        "price":              round(price, 6),
        "stop_loss":          round(stop_loss, 6) if stop_loss else 0,
        "take_profit":        round(take_profit, 6) if take_profit else 0,
        "risk_reward":        risk_reward,
        "atr":                round(atr_val or 0, 6),
        "vwap":               round(main.get("vwap") or 0, 4),
        "timeframes": {
            "1m":  sig_1m["signal"],
            "5m":  sig_5m["signal"],
            "15m": sig_15m["signal"],
        },
        "ob_signal":          ob_signal,
        "ob_reasons":         ob_analysis.get("ob_reasons", []),
        "market_regime":      regime_info.get("regime", "UNKNOWN"),
        "daily_bias":         bias_info.get("bias", "NEUTRAL"),
        "news_sentiment":     news_info.get("sentiment", "UNKNOWN"),
        "news_score":         news_info.get("news_score", 0),
        "news_risk_level":    news_info.get("risk_level", "CLEAR"),
        "news_safe":          news_info.get("safe", True),
        "reasons":            [mtf_agreement] + ob_analysis.get("ob_reasons", []) + main.get("reasons", []),
    }
    return result


def grade_signal(sig: dict) -> str:
    """
    Grade every directional signal A / B / C — regardless of whether it
    passes the hard gates. Used by the trade journal to classify ALL signals
    so we can compare grade performance after 50-100 logged entries.

    A-Grade : Current strict rules (conf≥70, score≥11, ADX≥25, vol≥1.0, all 3 TF)
    B-Grade : Relaxed rules   (conf≥55, score≥9,  ADX≥20, vol≥0.8, 2/3 TF)
    C-Grade : Everything else with a directional signal
    """
    if sig.get("signal", "HOLD") == "HOLD":
        return "NONE"

    signal    = sig["signal"]
    conf      = int(str(sig.get("confidence", "0%")).replace("%", ""))
    score_str = str(sig.get("score", "0/16"))
    score     = int(score_str.split("/")[0]) if "/" in score_str else 0
    adx       = sig.get("adx") or 0
    vol       = sig.get("vol_ratio") or 0
    tf        = sig.get("timeframes", {})
    tf_vals   = [tf.get("15m"), tf.get("1h"), tf.get("4h")]
    tf_agree  = tf_vals.count(signal)

    g = GRADE_A
    if (conf >= g["min_confidence"] and score >= g["min_score"]
            and adx >= g["min_adx"] and vol >= g["min_volume"]
            and tf_agree >= g["min_tf"]):
        return "A"

    g = GRADE_B
    if (conf >= g["min_confidence"] and score >= g["min_score"]
            and adx >= g["min_adx"] and vol >= g["min_volume"]
            and tf_agree >= g["min_tf"]):
        return "B"

    return "C"


def passes_filters_detail(sig: dict) -> tuple[bool, list[str]]:
    """
    Same logic as passes_filters() but returns (passed: bool, reasons: list[str]).
    Every blocking reason is captured so we can log exactly what rejected the signal.
    This is the engine behind the blocked_signals.json log.
    """
    if sig.get("signal") == "HOLD":
        return False, ["Signal is HOLD"]

    signal    = sig["signal"]
    conf      = int(str(sig.get("confidence", "0%")).replace("%", ""))
    score_num = int(str(sig.get("score", "0/16")).split("/")[0])
    vol       = sig.get("vol_ratio") or 0
    macd      = sig.get("macd_hist") or 0
    rsi       = sig.get("rsi") or 50
    adx       = sig.get("adx") or 0
    tf        = sig.get("timeframes", {})
    tf_vals   = [tf.get("15m"), tf.get("1h"), tf.get("4h")]
    hour_utc  = datetime.now(timezone.utc).hour
    daily_bias = sig.get("daily_bias", "NEUTRAL")
    ob_signal  = sig.get("ob_signal", "HOLD")

    reasons = []

    if conf < MIN_CONFIDENCE:
        reasons.append(f"Confidence {conf}% < {MIN_CONFIDENCE}% minimum")
    if score_num < MIN_SCORE:
        reasons.append(f"Score {score_num}/16 < {MIN_SCORE} minimum")
    if vol < MIN_VOL_RATIO:
        reasons.append(f"Volume {vol:.2f}x < {MIN_VOL_RATIO}x minimum")
    if not (TRADE_HRS_UTC[0] <= hour_utc < TRADE_HRS_UTC[1]):
        reasons.append(f"Outside trading hours (UTC {hour_utc}:00, window {TRADE_HRS_UTC[0]}-{TRADE_HRS_UTC[1]})")
    if tf_vals.count(signal) < 3:
        reasons.append(f"Only {tf_vals.count(signal)}/3 timeframes agree (15m={tf.get('15m')} 1h={tf.get('1h')} 4h={tf.get('4h')})")
    if signal == "BUY"  and macd <= 0:
        reasons.append(f"MACD {macd:.6f} opposes BUY signal")
    if signal == "SELL" and macd >= 0:
        reasons.append(f"MACD {macd:.6f} opposes SELL signal")
    if signal == "BUY"  and not (MIN_RSI_BUY <= rsi <= MAX_RSI_BUY):
        reasons.append(f"RSI {rsi:.1f} outside BUY zone ({MIN_RSI_BUY}-{MAX_RSI_BUY})")
    if signal == "SELL" and not (MIN_RSI_SELL <= rsi <= MAX_RSI_SELL):
        reasons.append(f"RSI {rsi:.1f} outside SELL zone ({MIN_RSI_SELL}-{MAX_RSI_SELL})")
    if adx < ADX_MIN:
        reasons.append(f"ADX {adx:.1f} < {ADX_MIN} minimum (weak trend)")
    if not sig.get("stop_loss") or not sig.get("take_profit"):
        reasons.append("No SL/TP calculated")
    if not sig.get("regime_tradeable", True):
        reasons.append(f"Market regime blocked: {sig.get('regime_reason', 'UNKNOWN')}")
    if signal == "BUY"  and daily_bias == "BEARISH":
        reasons.append("Daily bias BEARISH blocks BUY")
    if signal == "SELL" and daily_bias == "BULLISH":
        reasons.append("Daily bias BULLISH blocks SELL")
    if signal == "BUY"  and ob_signal == "SELL":
        reasons.append("Order book SELL pressure blocks BUY")
    if signal == "SELL" and ob_signal == "BUY":
        reasons.append("Order book BUY pressure blocks SELL")
    if NEWS_BLOCK_ENABLED and not sig.get("news_safe", True):
        reasons.append(f"News block: {sig.get('news_reason', 'risk event')}")

    return len(reasons) == 0, reasons


def log_blocked_signal(sig: dict, reasons: list[str]):
    """
    Record every rejected directional signal with the exact reason(s) it failed.
    After 50-100 entries, /blocked/stats will tell you which filter
    is blocking the most potentially-profitable signals.
    """
    global blocked_log
    symbol = sig.get("symbol", "")
    signal = sig.get("signal", "HOLD")
    if signal == "HOLD":
        return
    now = datetime.now(timezone.utc)

    # Deduplicate: skip same symbol+direction blocked in last 30 min
    thirty_ago = (now - timedelta(minutes=30)).isoformat()
    for b in blocked_log[-200:]:  # only scan recent entries
        if (b["symbol"] == symbol and b["signal"] == signal
                and b["logged_at"] > thirty_ago):
            return

    tf = sig.get("timeframes", {})
    blocked_log.append({
        "id":            f"BLK_{symbol}_{now.strftime('%Y%m%d_%H%M')}",
        "logged_at":     now.isoformat(),
        "date":          now.strftime("%Y-%m-%d"),
        "time_utc":      now.strftime("%H:%M"),
        "symbol":        symbol,
        "signal":        signal,
        "grade":         grade_signal(sig),
        # ── Filter values at rejection time ──────────────────────────
        "confidence":    sig.get("confidence", "0%"),
        "score":         sig.get("score", "0/16"),
        "adx":           round(sig.get("adx") or 0, 2),
        "vol_ratio":     round(sig.get("vol_ratio") or 0, 2),
        "rsi":           round(sig.get("rsi") or 0, 2),
        "macd":          round(sig.get("macd_hist") or 0, 6),
        "tf_15m":        tf.get("15m", "-"),
        "tf_1h":         tf.get("1h", "-"),
        "tf_4h":         tf.get("4h", "-"),
        "daily_bias":    sig.get("daily_bias", "NEUTRAL"),
        "market_regime": sig.get("market_regime", "UNKNOWN"),
        "ob_signal":     sig.get("ob_signal", "HOLD"),
        "news_safe":     sig.get("news_safe", True),
        # ── Why it was blocked ────────────────────────────────────────
        "blocked_reasons":  reasons,
        "blocked_by":       _primary_blocker(reasons),
        "reason_count":     len(reasons),
        # ── Price context — fill in later to assess missed move ───────
        "entry_price":   round(sig.get("price", 0), 6),
        "stop_loss":     round(sig.get("stop_loss") or 0, 6),
        "take_profit":   round(sig.get("take_profit") or 0, 6),
        # ── Outcome tracking (updated by update_blocked_outcomes) ─────
        "price_1h":      None,
        "price_4h":      None,
        "price_24h":     None,
        "move_1h_pct":   None,   # would this have been profitable at 1h?
        "move_4h_pct":   None,
        "move_24h_pct":  None,
        "max_profit_pct":   None,
        "max_adverse_pct":  None,
        "would_have_won":   None,   # True/False — did price hit TP before SL?
    })
    # Keep log bounded — last 500 blocked signals
    if len(blocked_log) > 500:
        blocked_log = blocked_log[-500:]
    save_blocked_log(blocked_log)


def _primary_blocker(reasons: list[str]) -> str:
    """Classify the single most significant blocking reason for quick stats."""
    keywords = [
        ("Confidence",       "confidence"),
        ("Score",            "score"),
        ("Volume",           "volume"),
        ("ADX",              "adx"),
        ("timeframes",       "tf_alignment"),
        ("MACD",             "macd"),
        ("RSI",              "rsi"),
        ("trading hours",    "outside_hours"),
        ("regime",           "market_regime"),
        ("Daily bias",       "daily_bias"),
        ("Order book",       "orderbook"),
        ("News",             "news"),
    ]
    for text, key in keywords:
        if any(text.lower() in r.lower() for r in reasons):
            return key
    return "other"


def passes_scalp_filters(sig: dict) -> tuple[bool, list[str]]:
    """
    Filter set for scalp signals.
    Requires 2/3 TF agreement (not all 3) — 1m is too noisy to require all 3.
    All other hard blocks still apply.
    """
    if sig.get("signal", "HOLD") == "HOLD":
        return False, ["Signal is HOLD"]

    signal    = sig["signal"]
    conf      = int(str(sig.get("confidence", "0%")).replace("%", ""))
    score_num = int(str(sig.get("score", "0/16")).split("/")[0])
    vol       = sig.get("vol_ratio") or 0
    adx       = sig.get("adx") or 0
    tf        = sig.get("timeframes", {})
    tf_vals   = [tf.get("1m"), tf.get("5m"), tf.get("15m")]

    reasons = []
    if conf      < SCALP_MIN_CONFIDENCE:
        reasons.append(f"Confidence {conf}% < {SCALP_MIN_CONFIDENCE}% scalp minimum")
    if score_num < SCALP_MIN_SCORE:
        reasons.append(f"Score {score_num}/16 < {SCALP_MIN_SCORE} scalp minimum")
    if vol       < SCALP_MIN_VOL_RATIO:
        reasons.append(f"Volume {vol:.2f}x < {SCALP_MIN_VOL_RATIO}x scalp minimum")
    if adx       < SCALP_ADX_MIN:
        reasons.append(f"ADX {adx:.1f} < {SCALP_ADX_MIN} scalp minimum")
    if tf_vals.count(signal) < 2:                          # relaxed: 2/3 TF (not all 3)
        reasons.append(f"Only {tf_vals.count(signal)}/3 scalp TF agree (need at least 2)")
    if sig.get("news_blocked"):
        reasons.append(f"News blocked: {sig.get('news_risk_level','?')}")
    if sig.get("regime_blocked"):
        reasons.append(f"Market regime blocked")
    if sig.get("sr_blocked"):
        reasons.append(f"S/R block: price too close to S/R level (scalp threshold 0.3%)")

    return len(reasons) == 0, reasons


def log_scalp_signal(sig: dict, passed: bool, reasons: list[str], force_grade: str = None):
    """
    Log scalp signal to:
    - scalp_log ALWAYS (for grade journal — same as swing approach)
    - scalp_blocked additionally if failed (for blocked signal tracking)
    force_grade: pre-computed grade to avoid calling grade_signal twice.
    """
    global scalp_log, scalp_blocked
    signal = sig.get("signal") or sig.get("pre_block_signal", "HOLD")
    if signal == "HOLD":
        return
    now = datetime.now(timezone.utc)
    tf  = sig.get("timeframes", {})

    entry = {
        "id":            f"SCALP_{sig.get('symbol','')}_{now.strftime('%Y%m%d_%H%M%S')}",
        "mode":          "SCALP",
        "logged_at":     now.isoformat(),
        "date":          now.strftime("%Y-%m-%d"),
        "time_utc":      now.strftime("%H:%M"),
        "symbol":        sig.get("symbol", ""),
        "signal":        signal,
        "grade":         force_grade or grade_signal({**sig, "signal": signal}),
        "trade_allowed": "YES" if passed else "NO",
        "blocked_by":    _primary_blocker(reasons) if not passed else "none",
        "blocked_reasons": reasons if not passed else [],
        # Filter values
        "confidence":    sig.get("confidence", "0%"),
        "score":         sig.get("score", "0/16"),
        "adx":           round(sig.get("adx") or 0, 2),
        "vol_ratio":     round(sig.get("vol_ratio") or 0, 2),
        "rsi":           round(sig.get("rsi") or 0, 2),
        "tf_1m":         tf.get("1m", "-"),
        "tf_5m":         tf.get("5m", "-"),
        "tf_15m":        tf.get("15m", "-"),
        # Entry/exit
        "entry_price":   round(sig.get("price", 0), 6),
        "stop_loss":     round(sig.get("stop_loss") or 0, 6),
        "take_profit":   round(sig.get("take_profit") or 0, 6),
        "risk_reward":   sig.get("risk_reward", "N/A"),
        "nearest_support":    round(sig.get("nearest_support") or 0, 6),
        "nearest_resistance": round(sig.get("nearest_resistance") or 0, 6),
        # Outcome tracking
        "status":        "OPEN",
        "outcome":       "-",
        "exit_price":    None,
        "exit_time":     None,
        "pnl_pct":       None,
        "hours_open":    0,
        "price_1h":      None,
        "pnl_1h":        None,
        "price_4h":      None,
        "pnl_4h":        None,
        "max_profit_pct":   None,
        "max_drawdown_pct": None,
        "would_have_won":   None,
        # Context
        "market_regime":    sig.get("market_regime", "UNKNOWN"),
        "news_sentiment":   sig.get("news_sentiment", "UNKNOWN"),
        "ob_signal":        sig.get("ob_signal", "HOLD"),
    }

    # Deduplicate — skip same symbol+direction in last 15 min for scalp
    fifteen_ago = (now - timedelta(minutes=15)).isoformat()

    for s in scalp_log[-100:]:
        if (s["symbol"] == entry["symbol"] and s["signal"] == signal
                and s["logged_at"] > fifteen_ago):
            return

    # Always log to scalp_log for grade journal (regardless of passed/failed)
    scalp_log.append(entry)
    if len(scalp_log) > 500:
        scalp_log[:] = scalp_log[-500:]
    save_scalp_log(scalp_log)

    # Additionally log to scalp_blocked for blocked signal tracking
    if not passed:
        scalp_blocked.append(entry)
        if len(scalp_blocked) > 500:
            scalp_blocked[:] = scalp_blocked[-500:]
        save_scalp_blocked(scalp_blocked)


def update_scalp_outcomes():
    """Track scalp signal outcomes — same as swing but uses 1h/4h snapshots."""
    global scalp_log
    changed = False
    now_utc = datetime.now(timezone.utc)

    for sig in scalp_log:
        if sig.get("status") != "OPEN":
            continue
        entry = sig.get("entry_price", 0)
        sl    = sig.get("stop_loss", 0)
        tp    = sig.get("take_profit", 0)
        if not entry or not sl or not tp or sl == 0 or tp == 0:
            continue
        try:
            import requests as req
            r     = req.get(f"https://api.mexc.com/api/v3/ticker/price?symbol={sig['symbol']}", timeout=5)
            price = float(r.json()["price"])
        except:
            continue

        direction  = sig["signal"]
        logged_at  = datetime.fromisoformat(sig["logged_at"])
        hours_open = round((now_utc - logged_at).total_seconds() / 3600, 1)
        sig["hours_open"] = hours_open

        if direction == "BUY":
            move_pct = round(((price - entry) / entry) * 100, 4)
        else:
            move_pct = round(((entry - price) / entry) * 100, 4)

        sig["max_profit_pct"]   = round(max(sig.get("max_profit_pct") or 0, move_pct), 4)
        sig["max_drawdown_pct"] = round(min(sig.get("max_drawdown_pct") or 0, move_pct), 4)

        if hours_open >= 1 and sig.get("pnl_1h") is None:
            sig["price_1h"] = round(price, 6); sig["pnl_1h"] = move_pct; changed = True
        if hours_open >= 4 and sig.get("pnl_4h") is None:
            sig["price_4h"] = round(price, 6); sig["pnl_4h"] = move_pct; changed = True

        outcome = None; exit_price = None
        if direction == "BUY":
            if price <= sl: outcome = "LOSS"; exit_price = sl
            elif price >= tp: outcome = "WIN";  exit_price = tp
        else:
            if price >= sl: outcome = "LOSS"; exit_price = sl
            elif price <= tp: outcome = "WIN";  exit_price = tp

        # Scalp expires after 4h (not 24h like swing)
        if not outcome and hours_open >= 4:
            outcome = "EXPIRED"; exit_price = price

        if outcome:
            sig["status"]     = "CLOSED"
            sig["outcome"]    = outcome
            sig["exit_price"] = round(exit_price, 6)
            sig["exit_time"]  = now_utc.strftime("%Y-%m-%d %H:%M")
            if direction == "BUY":
                sig["pnl_pct"] = round(((exit_price - entry) / entry) * 100, 4)
            else:
                sig["pnl_pct"] = round(((entry - exit_price) / entry) * 100, 4)
            changed = True

    if changed:
        save_scalp_log(scalp_log)


def passes_filters(sig: dict) -> bool:
    """Hard gates — signal must pass ALL to be logged. No exceptions."""
    if sig.get("signal") == "HOLD":
        return False

    signal    = sig["signal"]
    conf      = int(sig.get("confidence", "0%").replace("%", ""))
    score_num = int(sig.get("score", "0/16").split("/")[0])
    vol       = sig.get("vol_ratio") or 0
    macd      = sig.get("macd_hist") or 0
    rsi       = sig.get("rsi") or 50
    adx       = sig.get("adx") or 0
    tf        = sig.get("timeframes", {})
    tf_vals   = [tf.get("15m"), tf.get("1h"), tf.get("4h")]
    hour_utc  = datetime.now(timezone.utc).hour

    # ── Hard blocks — any one = instant reject ─────────────────────────
    if conf      < MIN_CONFIDENCE:                    return False
    if score_num < MIN_SCORE:                         return False
    if vol       < MIN_VOL_RATIO:                     return False
    if not (TRADE_HRS_UTC[0] <= hour_utc < TRADE_HRS_UTC[1]): return False
    if tf_vals.count(signal) < 3:                     return False  # all 3 TF must agree
    if signal == "BUY"  and macd <= 0:                return False  # MACD must confirm
    if signal == "SELL" and macd >= 0:                return False
    if signal == "BUY"  and not (MIN_RSI_BUY  <= rsi <= MAX_RSI_BUY):  return False
    if signal == "SELL" and not (MIN_RSI_SELL <= rsi <= MAX_RSI_SELL): return False
    if adx < ADX_MIN:                                 return False  # uses ADX_MIN constant
    if not sig.get("stop_loss") or not sig.get("take_profit"): return False
    if not sig.get("regime_tradeable", True):         return False
    daily_bias = sig.get("daily_bias", "NEUTRAL")
    if signal == "BUY"  and daily_bias == "BEARISH":  return False
    if signal == "SELL" and daily_bias == "BULLISH":  return False
    ob_signal = sig.get("ob_signal", "HOLD")
    if signal == "BUY"  and ob_signal == "SELL":      return False
    if signal == "SELL" and ob_signal == "BUY":       return False
    # News safety — block if high-impact event or risk headline
    if NEWS_BLOCK_ENABLED and not sig.get("news_safe", True): return False
    return True


def log_signal_entry(sig: dict, force_grade: str = None):
    """Log a new signal to the in-memory + file log.

    Every directional signal is graded A/B/C even if it doesn't pass hard
    gates (call with force_grade="B" or "C" from the all-signal journal path).
    Only A-grade signals trigger Telegram; all grades are logged for analysis.
    """
    global signal_log
    symbol = sig.get("symbol", "")
    signal = sig.get("signal", "HOLD")
    if signal == "HOLD":
        return
    now = datetime.now(timezone.utc)

    grade = force_grade or grade_signal(sig)

    # Deduplicate — skip same symbol+direction+grade open in last 2h
    two_hrs_ago = (now - timedelta(hours=2)).isoformat()
    for s in signal_log:
        if (s["symbol"] == symbol and s["signal"] == signal
                and s.get("grade") == grade
                and s["status"] == "OPEN" and s["logged_at"] > two_hrs_ago):
            return

    tf = sig.get("timeframes", {})
    entry = {
        "id":           f"{symbol}_{grade}_{now.strftime('%Y%m%d_%H%M')}",
        "logged_at":    now.isoformat(),
        "date":         now.strftime("%Y-%m-%d"),
        "time_utc":     now.strftime("%H:%M"),
        "symbol":       symbol,
        "signal":       signal,
        "grade":        grade,
        # ── Signal filters at time of logging ────────────────────────
        "confidence":   sig.get("confidence", "0%"),
        "score":        sig.get("score", "0/16"),
        "adx":          round(sig.get("adx") or 0, 2),
        "vol_ratio":    round(sig.get("vol_ratio") or 0, 2),
        "tf_15m":       tf.get("15m", "-"),
        "tf_1h":        tf.get("1h", "-"),
        "tf_4h":        tf.get("4h", "-"),
        "trade_allowed": "YES" if passes_filters(sig) else "NO",
        # ── Entry / risk management ───────────────────────────────────
        "entry_price":  round(sig.get("price", 0), 6),
        "stop_loss":    round(sig.get("stop_loss") or 0, 6),
        "take_profit":  round(sig.get("take_profit") or 0, 6),
        "risk_reward":  sig.get("risk_reward", "N/A"),
        "rsi":          round(sig.get("rsi") or 0, 2),
        "macd":         round(sig.get("macd_hist") or 0, 6),
        "fib_618":      round(sig.get("fib_618") or 0, 6),
        "fib_500":      round(sig.get("fib_500") or 0, 6),
        "fib_382":      round(sig.get("fib_382") or 0, 6),
        "nearest_support":    round(sig.get("nearest_support") or 0, 6),
        "nearest_resistance": round(sig.get("nearest_resistance") or 0, 6),
        # ── Outcome tracking ─────────────────────────────────────────
        "status":       "OPEN",
        "outcome":      "-",
        "exit_price":   None,
        "exit_time":    None,
        "pnl_pct":      None,
        "hours_open":   0,
        # After-signal snapshots (filled in by update_signal_outcomes)
        "price_1h":     None,   # price 1h after signal
        "price_4h":     None,   # price 4h after signal
        "price_24h":    None,   # price 24h after signal
        "pnl_1h":       None,   # % move vs entry at 1h mark
        "pnl_4h":       None,
        "pnl_24h":      None,
        "max_profit_pct":   None,   # peak favourable move during trade
        "max_drawdown_pct": None,   # peak adverse move during trade
        # ── Context at signal time ────────────────────────────────────
        "breakout_phase":    sig.get("breakout_phase", 0),
        "breakout_status":   sig.get("breakout_status", "NONE"),
        "breakout_target":   sig.get("breakout_target"),
        "breakout_move_pct": sig.get("breakout_move_pct", 0),
        "market_regime":     sig.get("market_regime", "UNKNOWN"),
        "daily_bias":        sig.get("daily_bias", "NEUTRAL"),
        "news_sentiment":    sig.get("news_sentiment", "UNKNOWN"),
        "news_score":        sig.get("news_score", 0),
        "news_risk_level":   sig.get("news_risk_level", "CLEAR"),
    }
    signal_log.append(entry)
    save_signal_log(signal_log)


def update_signal_outcomes():
    """
    Check open signals against current price.
    - Marks WIN / LOSS / EXPIRED when SL/TP hit or 24h elapsed.
    - Records price snapshots at 1h, 4h, 24h after signal.
    - Tracks max_profit_pct (best favourable move) and max_drawdown_pct
      (worst adverse move) throughout the trade lifetime.
    """
    global signal_log
    changed = False
    for sig in signal_log:
        if sig["status"] != "OPEN":
            continue
        try:
            import requests as req
            r     = req.get(f"https://api.mexc.com/api/v3/ticker/price?symbol={sig['symbol']}", timeout=5)
            price = float(r.json()["price"])
        except:
            continue

        direction  = sig["signal"]
        sl         = sig["stop_loss"]
        tp         = sig["take_profit"]
        entry      = sig["entry_price"]

        # Skip outcome tracking if SL/TP/entry are missing or zero
        if not sl or not tp or not entry or sl == 0 or tp == 0:
            continue

        logged_at  = datetime.fromisoformat(sig["logged_at"])
        now_utc    = datetime.now(timezone.utc)
        hours_open = round((now_utc - logged_at).total_seconds() / 3600, 1)
        sig["hours_open"] = hours_open

        # ── Directional move % from entry ────────────────────────────
        if direction == "BUY":
            move_pct = round(((price - entry) / entry) * 100, 4)
        else:  # SELL
            move_pct = round(((entry - price) / entry) * 100, 4)

        # ── Max profit / max drawdown tracking ───────────────────────
        prev_max = sig.get("max_profit_pct") or 0
        prev_min = sig.get("max_drawdown_pct") or 0
        sig["max_profit_pct"]   = round(max(prev_max, move_pct), 4)
        sig["max_drawdown_pct"] = round(min(prev_min, move_pct), 4)

        # ── Snapshot at 1h / 4h / 24h after signal ───────────────────
        if hours_open >= 1 and sig.get("pnl_1h") is None:
            sig["price_1h"] = round(price, 6)
            sig["pnl_1h"]   = move_pct
            changed = True
        if hours_open >= 4 and sig.get("pnl_4h") is None:
            sig["price_4h"] = round(price, 6)
            sig["pnl_4h"]   = move_pct
            changed = True
        if hours_open >= 24 and sig.get("pnl_24h") is None:
            sig["price_24h"] = round(price, 6)
            sig["pnl_24h"]   = move_pct
            changed = True

        # ── Outcome detection ─────────────────────────────────────────
        outcome    = None
        exit_price = None

        if direction == "BUY":
            if price <= sl:   outcome = "LOSS"; exit_price = sl
            elif price >= tp: outcome = "WIN";  exit_price = tp
        elif direction == "SELL":
            if price >= sl:   outcome = "LOSS"; exit_price = sl
            elif price <= tp: outcome = "WIN";  exit_price = tp

        if not outcome and hours_open >= MAX_SIGNAL_AGE:
            outcome = "EXPIRED"; exit_price = price

        if outcome:
            sig["status"]     = "CLOSED"
            sig["outcome"]    = outcome
            sig["exit_price"] = round(exit_price, 6)
            sig["exit_time"]  = now_utc.strftime("%Y-%m-%d %H:%M")
            if direction == "BUY":
                sig["pnl_pct"] = round(((exit_price - entry) / entry) * 100, 4)
            else:
                sig["pnl_pct"] = round(((entry - exit_price) / entry) * 100, 4)
            changed = True

    if changed:
        save_signal_log(signal_log)




# ── Excel generator (returns bytes for download) ──────────────────────

def update_blocked_outcomes():
    """
    For every blocked signal that has a valid SL/TP, check whether
    price subsequently hit TP or SL — answering "would this have won?"
    This is what makes the blocked log analytically useful.
    """
    global blocked_log
    changed = False
    now_utc = datetime.now(timezone.utc)

    for b in blocked_log:
        # Skip if already fully evaluated or no SL/TP
        if b.get("would_have_won") is not None:
            continue
        entry = b.get("entry_price", 0)
        sl    = b.get("stop_loss", 0)
        tp    = b.get("take_profit", 0)
        if not entry or not sl or not tp:
            continue

        try:
            import requests as req
            r     = req.get(f"https://api.mexc.com/api/v3/ticker/price?symbol={b['symbol']}", timeout=5)
            price = float(r.json()["price"])
        except:
            continue

        direction  = b["signal"]
        logged_at  = datetime.fromisoformat(b["logged_at"])
        hours_open = (now_utc - logged_at).total_seconds() / 3600

        # Directional move %
        if direction == "BUY":
            move_pct = round(((price - entry) / entry) * 100, 4)
        else:
            move_pct = round(((entry - price) / entry) * 100, 4)

        # Max profit / adverse tracking
        prev_max = b.get("max_profit_pct") or 0
        prev_adv = b.get("max_adverse_pct") or 0
        b["max_profit_pct"]  = round(max(prev_max, move_pct), 4)
        b["max_adverse_pct"] = round(min(prev_adv, move_pct), 4)

        # Snapshot prices
        if hours_open >= 1  and b.get("move_1h_pct")  is None:
            b["price_1h"]    = round(price, 6)
            b["move_1h_pct"] = move_pct
            changed = True
        if hours_open >= 4  and b.get("move_4h_pct")  is None:
            b["price_4h"]    = round(price, 6)
            b["move_4h_pct"] = move_pct
            changed = True
        if hours_open >= 24 and b.get("move_24h_pct") is None:
            b["price_24h"]    = round(price, 6)
            b["move_24h_pct"] = move_pct
            changed = True

        # Determine if it would have won (TP hit before SL within 24h)
        if hours_open >= 24 or b.get("move_24h_pct") is not None:
            if direction == "BUY":
                would_win = price >= tp
            else:
                would_win = price <= tp
            b["would_have_won"] = would_win
            changed = True

    if changed:
        save_blocked_log(blocked_log)



class _NullWs:
    """Absorbs all worksheet calls when a sheet is skipped (wrong mode)."""
    def write(self, *a, **kw): pass
    def set_tab_color(self, *a, **kw): pass
    def freeze_panes(self, *a, **kw): pass
    def set_column(self, *a, **kw): pass
    def set_row(self, *a, **kw): pass
    def merge_range(self, *a, **kw): pass
    def autofilter(self, *a, **kw): pass
    def conditional_format(self, *a, **kw): pass
_NULL_WS = _NullWs()

def generate_excel_bytes(mode: str = "swing") -> bytes:
    import xlsxwriter
    import pandas as pd

    update_signal_outcomes()
    signals = signal_log

    buf = io.BytesIO()
    df  = pd.DataFrame(signals) if signals else pd.DataFrame()

    wb = xlsxwriter.Workbook(buf, {"in_memory": True, "nan_inf_to_errors": True})

    title  = wb.add_format({"bold":True,"font_size":13,"font_color":"#58A6FF","bg_color":"#0D1117"})
    hdr    = wb.add_format({"bold":True,"bg_color":"#161B22","font_color":"#FFFFFF","border":1,"align":"center"})
    win_f  = wb.add_format({"bg_color":"#0D3321","font_color":"#3FB950","border":1})
    loss_f = wb.add_format({"bg_color":"#3D1212","font_color":"#F85149","border":1})
    open_f = wb.add_format({"bg_color":"#1C2333","font_color":"#F0B429","border":1})
    exp_f  = wb.add_format({"bg_color":"#21262D","font_color":"#8B949E","border":1})
    neu    = wb.add_format({"bg_color":"#161B22","font_color":"#FFFFFF","border":1})
    grn    = wb.add_format({"bold":True,"bg_color":"#161B22","font_color":"#3FB950","border":1})
    red    = wb.add_format({"bold":True,"bg_color":"#161B22","font_color":"#F85149","border":1})
    ylw    = wb.add_format({"bold":True,"bg_color":"#161B22","font_color":"#F0B429","border":1})
    subhdr = wb.add_format({"bold":True,"bg_color":"#21262D","font_color":"#58A6FF","border":1})

    def rfmt(status):
        return {"WIN":win_f,"LOSS":loss_f,"OPEN":open_f,"EXPIRED":exp_f}.get(status, neu)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Sheet 1: All Signals (swing only) ───────────────────────────
    ws1 = wb.add_worksheet("All Signals") if mode == "swing" else _NULL_WS
    ws1.set_tab_color("#58A6FF"); ws1.freeze_panes(3,0)
    ws1.write(0,0,f"MEXC Bot — All Signals Tracker  ({now_str})",title)

    cols = [
        ("Date","date",10),("Time UTC","time_utc",8),("Symbol","symbol",10),
        ("Signal","signal",7),("Grade","grade",7),
        ("Score","score",8),("Confidence","confidence",10),
        ("ADX","adx",7),("Vol Ratio","vol_ratio",9),
        ("15m","tf_15m",6),("1h","tf_1h",6),("4h","tf_4h",6),
        ("Trade Allowed?","trade_allowed",13),
        ("Entry","entry_price",13),("Stop Loss","stop_loss",13),("Take Profit","take_profit",13),
        ("R:R","risk_reward",8),("RSI","rsi",7),
        ("Fib 61.8%","fib_618",12),("Fib 50%","fib_500",10),("Fib 38.2%","fib_382",12),
        ("Support","nearest_support",13),("Resistance","nearest_resistance",13),
        ("Status","status",9),("Outcome","outcome",9),
        ("Exit Price","exit_price",13),("Exit Time","exit_time",16),
        ("Hours Open","hours_open",10),("PnL %","pnl_pct",9),
        # After-signal snapshots
        ("PnL @ 1h","pnl_1h",10),("PnL @ 4h","pnl_4h",10),("PnL @ 24h","pnl_24h",10),
        ("Max Profit %","max_profit_pct",13),("Max Drawdown %","max_drawdown_pct",15),
        ("Breakout Phase","breakout_phase",14),("Breakout Status","breakout_status",16),
        ("BO Target","breakout_target",14),("BO Move %","breakout_move_pct",12),
        ("Market Regime","market_regime",14),("Daily Bias","daily_bias",12),
    ]
    for c,(h,_,w) in enumerate(cols):
        ws1.set_column(c,c,w); ws1.write(2,c,h,hdr)

    if not df.empty:
        for r,row in enumerate(df.itertuples(),start=3):
            rf = rfmt(getattr(row,"status","-"))
            for c,(_,col,_) in enumerate(cols):
                val = getattr(row,col,"")
                if val is None: val = "-"
                # Prevent Excel formula injection and =#NUM! errors
                if isinstance(val, str) and val.startswith("="):
                    val = "'" + val  # escape as text
                ws1.write(r,c,val,rf)
    else:
        ws1.write(3,0,"No signals logged yet — signals appear here automatically",ylw)

    # ── Sheet 2: Open Signals ─────────────────────────────────────────
    ws2 = wb.add_worksheet("🟡 Open Signals") if mode == "swing" else _NULL_WS; ws2.set_tab_color("#F0B429"); ws2.freeze_panes(3,0)
    open_df = df[df["status"]=="OPEN"] if not df.empty and "status" in df.columns else pd.DataFrame()
    ws2.write(0,0,f"Currently Open — {len(open_df)} signals being tracked  ({now_str})",title)
    ocols = [("Date","date",10),("Time","time_utc",8),("Symbol","symbol",10),
             ("Signal","signal",7),("Score","score",8),("Conf","confidence",10),
             ("4H","tf_4h",6),("Entry","entry_price",13),
             ("SL","stop_loss",13),("TP","take_profit",13),
             ("R:R","risk_reward",8),("Hrs Open","hours_open",9),
             ("RSI","rsi",7),("Fib 61.8%","fib_618",12),("Support","nearest_support",12)]
    for c,(h,_,w) in enumerate(ocols):
        ws2.set_column(c,c,w); ws2.write(2,c,h,hdr)
    if open_df.empty:
        ws2.write(3,0,"No open signals right now",ylw)
    else:
        for r,row in enumerate(open_df.itertuples(),start=3):
            for c,(_,col,_) in enumerate(ocols):
                val=getattr(row,col,"")
                if val is None: val="-"
                ws2.write(r,c,val,open_f)

    # ── Sheet 3: Daily Summary ────────────────────────────────────────
    ws3 = wb.add_worksheet("Daily Summary") if mode == "swing" else _NULL_WS; ws3.set_tab_color("#3FB950"); ws3.freeze_panes(3,0)
    ws3.write(0,0,"Daily Signal Performance",title)
    dh=["Date","Total","Wins","Losses","Expired","Open","Win Rate %","Total PnL %","Best %","Worst %"]
    for c,h in enumerate(dh): ws3.set_column(c,c,13); ws3.write(2,c,h,hdr)

    if not df.empty and "date" in df.columns:
        closed_df = df[df["status"]=="CLOSED"]
        daily = df.groupby("date").apply(lambda g: pd.Series({
            "total":   len(g),
            "wins":    int((g["outcome"]=="WIN").sum()),
            "losses":  int((g["outcome"]=="LOSS").sum()),
            "expired": int((g["outcome"]=="EXPIRED").sum()),
            "open_ct": int((g["status"]=="OPEN").sum()),
            "wr":      round((g["outcome"]=="WIN").sum()/max((g["outcome"].isin(["WIN","LOSS"])).sum(),1)*100,1),
            "pnl":     round(g["pnl_pct"].dropna().sum(),2),
            "best":    round(g["pnl_pct"].dropna().max(),2) if g["pnl_pct"].notna().any() else 0,
            "worst":   round(g["pnl_pct"].dropna().min(),2) if g["pnl_pct"].notna().any() else 0,
        })).reset_index()
        for r,row in enumerate(daily.itertuples(),start=3):
            rf = win_f if row.pnl>=0 else loss_f
            ws3.write(r,0,str(row.date)[:10],rf); ws3.write(r,1,int(row.total),rf)
            ws3.write(r,2,int(row.wins),win_f); ws3.write(r,3,int(row.losses),loss_f)
            ws3.write(r,4,int(row.expired),exp_f); ws3.write(r,5,int(row.open_ct),open_f)
            ws3.write(r,6,f"{row.wr}%",grn if row.wr>=55 else (ylw if row.wr>=45 else red))
            ws3.write(r,7,f"{row.pnl}%",rf)
            ws3.write(r,8,f"+{row.best}%",grn); ws3.write(r,9,f"{row.worst}%",red)
    else:
        ws3.write(3,0,"No data yet",ylw)

    # ── Sheet 4: By Symbol ────────────────────────────────────────────
    ws4 = wb.add_worksheet("By Symbol") if mode == "swing" else _NULL_WS; ws4.set_tab_color("#F85149"); ws4.freeze_panes(3,0)
    ws4.write(0,0,"Signal Performance by Symbol",title)
    sh=["Symbol","Total","Wins","Losses","Win Rate","Total PnL %","Avg Win %","Avg Loss %","Best","Worst"]
    for c,h in enumerate(sh): ws4.set_column(c,c,13); ws4.write(2,c,h,hdr)
    if not df.empty and "symbol" in df.columns:
        closed_df = df[df["status"]=="CLOSED"]
        if not closed_df.empty:
            for r,(sym,g) in enumerate(closed_df.groupby("symbol"),start=3):
                wns=g[g["outcome"]=="WIN"]; lss=g[g["outcome"]=="LOSS"]
                wr2=round(len(wns)/len(g)*100,1) if len(g)>0 else 0
                rf=win_f if wr2>=50 else loss_f
                ws4.write(r,0,sym,rf); ws4.write(r,1,len(g),rf)
                ws4.write(r,2,len(wns),win_f); ws4.write(r,3,len(lss),loss_f)
                ws4.write(r,4,f"{wr2}%",grn if wr2>=55 else (ylw if wr2>=45 else red))
                ws4.write(r,5,f"{round(g['pnl_pct'].dropna().sum(),2)}%",rf)
                ws4.write(r,6,f"+{round(wns['pnl_pct'].mean(),2)}%" if not wns.empty else "N/A",grn)
                ws4.write(r,7,f"{round(lss['pnl_pct'].mean(),2)}%" if not lss.empty else "N/A",red)
                ws4.write(r,8,f"+{round(g['pnl_pct'].dropna().max(),2)}%",grn)
                ws4.write(r,9,f"{round(g['pnl_pct'].dropna().min(),2)}%",red)
        else:
            ws4.write(3,0,"No closed signals yet",ylw)
    else:
        ws4.write(3,0,"No data yet",ylw)

    # ── Sheet 5: Dashboard ────────────────────────────────────────────
    ws5 = wb.add_worksheet("📊 Dashboard") if mode == "swing" else _NULL_WS; ws5.set_tab_color("#58A6FF")
    ws5.set_column("A:A",35); ws5.set_column("B:B",30)
    ws5.write(0,0,"📊 MEXC Bot — Live Signal Tracker Dashboard",title)
    ws5.write(1,0,f"Generated: {now_str}",neu)

    total_all  = len(signal_log)
    closed_all = [s for s in signal_log if s["status"]=="CLOSED"]
    wins_all   = sum(1 for s in closed_all if s["outcome"]=="WIN")
    losses_all = sum(1 for s in closed_all if s["outcome"]=="LOSS")
    exp_all    = sum(1 for s in closed_all if s["outcome"]=="EXPIRED")
    open_all   = sum(1 for s in signal_log if s["status"]=="OPEN")
    wr_all     = round(wins_all/max(wins_all+losses_all,1)*100,1)
    tpnl       = round(sum(s["pnl_pct"] for s in closed_all if s["pnl_pct"] is not None),2)
    aw         = round(sum(s["pnl_pct"] for s in closed_all if s["outcome"]=="WIN" and s["pnl_pct"])/max(wins_all,1),2) if wins_all>0 else 0
    al         = round(sum(s["pnl_pct"] for s in closed_all if s["outcome"]=="LOSS" and s["pnl_pct"] is not None)/max(losses_all,1),2) if losses_all>0 else 0

    stats = [
        ("── LIVE SIGNAL TRACKER ─────────────────",""),
        ("📋 Total Signals Logged",    total_all),
        ("✅ Wins — TP Hit",           wins_all),
        ("❌ Losses — SL Hit",         losses_all),
        ("⏰ Expired (24h timeout)",   exp_all),
        ("🟡 Currently Open",          open_all),
        ("🎯 Win Rate (closed only)",  f"{wr_all}%"),
        ("💰 Total PnL % (all closed)",f"{tpnl}%"),
        ("📈 Average Win %",           f"{aw}%"),
        ("📉 Average Loss %",          f"{al}%"),
        ("",""),
        ("── ALL FIXES ACTIVE ────────────────────",""),
        ("✅ Min Confidence",           f"{MIN_CONFIDENCE}%  (testing: lowered from 70%)"),
        ("✅ Min Score",                f"{MIN_SCORE}/16  (testing: lowered from 11)"),
        ("✅ Min Volume",               f"{MIN_VOL_RATIO}x  (testing: lowered from 1.0x)"),
        ("✅ Min ADX",                  f"{ADX_MIN}  (testing: lowered from 25)"),
        ("✅ Time Filter",              f"{TRADE_HRS_UTC[0]}:00–{TRADE_HRS_UTC[1]}:00 UTC only"),
        ("✅ 4H Must Agree",            "ON — never trades against 4H trend"),
        ("✅ Fibonacci Entry Guide",    "Shows before entry — hides after"),
        ("✅ 4H Exit Warning",          "Only fires if 4H CHANGES after entry"),
        ("✅ Signal Age Limit",         f"{MAX_SIGNAL_AGE}h — auto-expire old signals"),
        ("✅ Market Regime Filter",      "ON — blocks RANGING + VOLATILE markets"),
        ("✅ Daily HTF Bias Gate",       "ON — only trades WITH daily trend"),
        ("✅ Breakout Detector",         "Phase 1 (compression) + Phase 2 (confirmed)"),
        ("✅ News Filter",               "Economic calendar + CryptoPanic sentiment"),
        ("",""),
        ("── SIGNAL HEALTH ───────────────────────",""),
    ]

    issues=[]
    if wr_all<50 and len(closed_all)>=10:
        issues.append(("❌ Win rate below 50%","Raise MIN_SCORE to 12 or MIN_CONFIDENCE to 75%"))
    if al and aw and abs(al)>aw:
        issues.append(("❌ Avg loss bigger than avg win","Tighten SL multiplier in indicators.py"))
    if tpnl<0 and len(closed_all)>=5:
        issues.append(("❌ Net negative PnL","Review signal quality — do NOT trade live yet"))
    if total_all<3:
        issues.append(("⚠️ Very few signals yet","Normal — signals accumulate over days"))
    if open_all>5:
        issues.append(("⚠️ Many signals still open","Price check may be delayed"))
    if not issues:
        if len(closed_all)>=5:
            issues.append(("✅ Bot signals look healthy!","Keep monitoring for consistency"))
        else:
            issues.append(("⏳ Still collecting data","Need 10+ closed signals to judge quality"))

    for r2,(lbl,val) in enumerate(stats+issues,start=3):
        if not lbl: continue
        sec=lbl.startswith("──")
        lf=subhdr if sec else (red if "❌" in lbl else (ylw if "⚠️" in lbl else (grn if "✅" in lbl else (ylw if "⏳" in lbl else neu))))
        isg=("Win Rate" in lbl and wr_all>=55) or ("PnL" in lbl and tpnl>=0)
        isb=("Win Rate" in lbl and wr_all<50 and len(closed_all)>=10) or ("PnL" in lbl and tpnl<0 and len(closed_all)>=5)
        vf=grn if isg else (red if isb else neu)
        ws5.write(r2,0,lbl,lf)
        if val!="": ws5.write(r2,1,str(val),vf)

    # ── Sheet 6: Blocked Signals ──────────────────────────────────────
    ws6 = wb.add_worksheet("🚫 Blocked Signals") if mode == "swing" else _NULL_WS; ws6.set_tab_color("#F85149"); ws6.freeze_panes(3,0)
    ws6.write(0, 0, f"Blocked Signals — What the Bot Rejected & Why  ({now_str})", title)
    ws6.write(1, 0, "HIGH missed-win rate on a filter = consider relaxing it", ylw)

    bcols = [
        ("Date","date",10),("Time","time_utc",8),("Symbol","symbol",10),
        ("Signal","signal",7),("Grade","grade",7),
        ("Blocked By","blocked_by",14),("# Reasons","reason_count",10),
        ("Conf","confidence",9),("Score","score",8),("ADX","adx",7),
        ("Vol","vol_ratio",7),("RSI","rsi",7),
        ("15m","tf_15m",6),("1h","tf_1h",6),("4h","tf_4h",6),
        ("Daily Bias","daily_bias",11),("OB Signal","ob_signal",9),
        ("Entry","entry_price",13),("SL","stop_loss",13),("TP","take_profit",13),
        ("Move@1h%","move_1h_pct",10),("Move@4h%","move_4h_pct",10),("Move@24h%","move_24h_pct",11),
        ("MaxProfit","max_profit_pct",11),("MaxAdverse","max_adverse_pct",12),
        ("Would've Won?","would_have_won",14),
        ("Reasons","blocked_reasons",50),
    ]
    for c,(h,_,w) in enumerate(bcols):
        ws6.set_column(c,c,w); ws6.write(2,c,h,hdr)

    if blocked_log:
        for r, row in enumerate(reversed(blocked_log[-300:]), start=3):
            primary = row.get("blocked_by","")
            wwon    = row.get("would_have_won")
            if wwon is True:   rf = loss_f   # blocked a winner — highlight red
            elif wwon is False: rf = win_f   # correctly blocked a loser — highlight green
            else:               rf = open_f  # not yet evaluated
            for c,(_,col,_) in enumerate(bcols):
                val = row.get(col,"")
                if isinstance(val, list): val = " | ".join(str(v) for v in val)
                if val is None: val = "-"
                if col == "would_have_won":
                    val = "✅ YES — missed winner" if wwon is True else ("❌ NO — good block" if wwon is False else "⏳ pending")
                ws6.write(r, c, val, rf)
    else:
        ws6.write(3, 0, "No blocked signals yet — populates automatically as scanner runs", ylw)

    # ── Per-filter summary table at the top right ─────────────────────
    ws6.write(0, 5, "Per-Filter Breakdown", subhdr)
    fh = ["Filter","Blocked","Evaluated","Would've Won","Missed Win Rate","Verdict"]
    for c,h in enumerate(fh): ws6.write(2, c+5, h, hdr)

    filter_keys = ["confidence","score","volume","adx","tf_alignment",
                   "macd","rsi","outside_hours","market_regime","daily_bias","news"]
    evaluated_all = [b for b in blocked_log if b.get("would_have_won") is not None]

    for fi, fk in enumerate(filter_keys):
        bf  = [b for b in blocked_log if b.get("blocked_by") == fk]
        ev  = [b for b in bf if b.get("would_have_won") is not None]
        ww  = sum(1 for b in ev if b.get("would_have_won"))
        mwr = round(ww/max(len(ev),1)*100,1) if ev else 0
        verd = ("⚠️ Relax?" if mwr >= 60 else ("🟡 Monitor" if mwr >= 45 else "✅ Good")) if len(ev)>=5 else "⏳ pending"
        rf2 = loss_f if mwr >= 60 else (ylw if mwr >= 45 else win_f)
        row_data = [fk, len(bf), len(ev), ww, f"{mwr}%", verd]
        for c, v in enumerate(row_data):
            ws6.write(3+fi, c+5, v, rf2)

    # ── Sheet 7: Grade Analysis ───────────────────────────────────────
    ws7 = wb.add_worksheet("🔬 Grade Analysis") if mode == "swing" else _NULL_WS; ws7.set_tab_color("#B963D4"); ws7.freeze_panes(4,0)
    ws7.set_column("A:A", 22); ws7.set_column("B:Z", 14)
    ws7.write(0, 0, "📊 A / B / C Grade Performance — Filter Optimisation Journal", title)
    ws7.write(1, 0, f"Generated: {now_str}", neu)
    ws7.write(2, 0, "A-Grade: Conf≥70 Score≥11 ADX≥25 Vol≥1.0x All3TF", subhdr)
    ws7.write(2, 3, "B-Grade: Conf≥55 Score≥9 ADX≥20 Vol≥0.8x 2/3TF", subhdr)
    ws7.write(2, 6, "C-Grade: Everything else (directional signals)", subhdr)

    gh = ["Grade","Total","Open","Closed","Wins","Losses","Expired",
          "Win Rate","Avg Profit","Avg Win","Avg Loss","Total PnL",
          "Avg PnL@1h","Avg PnL@4h","Avg PnL@24h","Avg MaxProfit","Avg MaxDD"]
    for c, h in enumerate(gh):
        ws7.write(3, c, h, hdr)

    directional_all = [s for s in signal_log if s.get("signal") in ("BUY","SELL")]
    for gi, grade in enumerate(["A","B","C"]):
        gs = _grade_stats(directional_all, grade)
        grade_fmt = win_f if grade == "A" else (open_f if grade == "B" else exp_f)
        row_vals = [
            gs["grade"], gs["total"], gs["open"], gs["closed"],
            gs["wins"], gs["losses"], gs["expired"],
            gs["win_rate"], gs["avg_profit"], gs["avg_win"], gs["avg_loss"], gs["total_pnl"],
            gs["avg_pnl_1h"], gs["avg_pnl_4h"], gs["avg_pnl_24h"],
            gs["avg_max_profit"], gs["avg_max_drawdown"],
        ]
        for c, v in enumerate(row_vals):
            ws7.write(4 + gi, c, v, grade_fmt)

    # Signal-by-signal journal table
    jcols = [
        ("Time","time_utc",8),("Symbol","symbol",10),("Dir","signal",6),("Grade","grade",6),
        ("Conf","confidence",9),("Score","score",8),("ADX","adx",7),("Vol","vol_ratio",7),
        ("15m","tf_15m",6),("1h","tf_1h",6),("4h","tf_4h",6),("Allowed?","trade_allowed",10),
        ("Entry","entry_price",13),("SL","stop_loss",13),("TP","take_profit",13),
        ("PnL%","pnl_pct",9),("@1h%","pnl_1h",9),("@4h%","pnl_4h",9),("@24h%","pnl_24h",9),
        ("MaxProfit","max_profit_pct",11),("MaxDD","max_drawdown_pct",11),
        ("Outcome","outcome",9),("Status","status",9),
    ]
    jstart = 9
    ws7.write(jstart - 1, 0, "── Full Signal Journal ──", subhdr)
    for c, (h, _, w) in enumerate(jcols):
        ws7.set_column(c, c, w); ws7.write(jstart, c, h, hdr)

    if directional_all:
        for r, row in enumerate(reversed(directional_all[-200:]), start=jstart+1):
            rf = rfmt(row.get("status","-"))
            allowed_f = grn if row.get("trade_allowed") == "YES" else red
            for c, (_, col, _) in enumerate(jcols):
                val = row.get(col, "")
                if val is None: val = "-"
                use_f = allowed_f if col == "trade_allowed" else rf
                ws7.write(r, c, val, use_f)
    else:
        ws7.write(jstart + 1, 0, "No signals logged yet — journal fills automatically", ylw)

    # ── Sheet 8: Scalp Signals ────────────────────────────────────────
    ws8 = wb.add_worksheet("⚡ Scalp Signals") if mode == "scalp" else _NULL_WS; ws8.set_tab_color("#F0B429"); ws8.freeze_panes(3,0)
    ws8.write(0, 0, f"⚡ Scalp Signals (1m+5m+15m)  •  Stricter Filters  •  {now_str}", title)
    ws8.write(1, 0, f"Filters: Conf≥{SCALP_MIN_CONFIDENCE}% Score≥{SCALP_MIN_SCORE} ADX≥{SCALP_ADX_MIN} Vol≥{SCALP_MIN_VOL_RATIO}x  All 3 TF must agree  •  SL=ATR×1 TP=ATR×2", neu)

    scols = [
        ("Date","date",10),("Time","time_utc",8),("Symbol","symbol",10),
        ("Signal","signal",7),("Grade","grade",7),("Allowed?","trade_allowed",10),
        ("Conf","confidence",9),("Score","score",8),("ADX","adx",7),("Vol","vol_ratio",7),
        ("1m","tf_1m",6),("5m","tf_5m",6),("15m","tf_15m",6),
        ("Entry","entry_price",13),("SL","stop_loss",13),("TP","take_profit",13),("R:R","risk_reward",8),
        ("RSI","rsi",7),("OB","ob_signal",7),
        ("Status","status",9),("Outcome","outcome",9),
        ("PnL%","pnl_pct",9),("@1h%","pnl_1h",9),("@4h%","pnl_4h",9),
        ("MaxProfit","max_profit_pct",11),("MaxDD","max_drawdown_pct",11),
        ("Regime","market_regime",14),("News","news_sentiment",12),
    ]
    for c,(h,_,w) in enumerate(scols):
        ws8.set_column(c,c,w); ws8.write(2,c,h,hdr)

    if scalp_log:
        for r, row in enumerate(reversed(scalp_log[-300:]), start=3):
            rf = rfmt(row.get("status","-"))
            allowed_f = grn if row.get("trade_allowed") == "YES" else red
            for c,(_,col,_) in enumerate(scols):
                val = row.get(col,"")
                if val is None: val = "-"
                use_f = allowed_f if col == "trade_allowed" else rf
                ws8.write(r, c, val, use_f)
    else:
        ws8.write(3, 0, "No scalp signals yet — scanner populates automatically", ylw)

    # ── Sheet 9: Scalp Blocked ────────────────────────────────────────
    ws9 = wb.add_worksheet("⚡🚫 Scalp Blocked") if mode == "scalp" else _NULL_WS; ws9.set_tab_color("#FF6B6B"); ws9.freeze_panes(3,0)
    ws9.write(0, 0, f"Scalp Blocked Signals — What Was Rejected & Why  ({now_str})", title)

    sbcols = [
        ("Date","date",10),("Time","time_utc",8),("Symbol","symbol",10),
        ("Signal","signal",7),("Grade","grade",7),("Blocked By","blocked_by",14),
        ("Conf","confidence",9),("Score","score",8),("ADX","adx",7),("Vol","vol_ratio",7),
        ("1m","tf_1m",6),("5m","tf_5m",6),("15m","tf_15m",6),
        ("Entry","entry_price",13),("SL","stop_loss",13),("TP","take_profit",13),
        ("MaxProfit","max_profit_pct",11),("MaxAdverse","max_drawdown_pct",11),
        ("Would've Won?","would_have_won",14),
        ("Reasons","blocked_reasons",50),
    ]
    for c,(h,_,w) in enumerate(sbcols):
        ws9.set_column(c,c,w); ws9.write(2,c,h,hdr)

    if scalp_blocked:
        for r, row in enumerate(reversed(scalp_blocked[-200:]), start=3):
            wwon = row.get("would_have_won")
            rf2  = loss_f if wwon is True else (win_f if wwon is False else open_f)
            for c,(_,col,_) in enumerate(sbcols):
                val = row.get(col,"")
                if isinstance(val, list): val = " | ".join(str(v) for v in val)
                if val is None: val = "-"
                if col == "would_have_won":
                    val = "✅ YES" if wwon is True else ("❌ NO" if wwon is False else "⏳")
                ws9.write(r, c, val, rf2)
    else:
        ws9.write(3, 0, "No scalp blocked signals yet", ylw)

    # ── Sheet 10: Scalp Stats ─────────────────────────────────────────
    ws10 = wb.add_worksheet("⚡📊 Scalp Stats") if mode == "scalp" else _NULL_WS; ws10.set_tab_color("#58A6FF")
    ws10.write(0, 0, f"Scalp Performance Summary  ({now_str})", title)
    ws10.set_column("A:A", 28); ws10.set_column("B:B", 16)

    scalp_closed  = [s for s in scalp_log if s.get("status") == "CLOSED"]
    scalp_wins    = [s for s in scalp_closed if s.get("outcome") == "WIN"]
    scalp_losses  = [s for s in scalp_closed if s.get("outcome") == "LOSS"]
    scalp_pnls    = [s["pnl_pct"] for s in scalp_closed if s.get("pnl_pct") is not None]
    scalp_wr      = round(len(scalp_wins)/max(len(scalp_wins)+len(scalp_losses),1)*100,1)

    scalp_stats = [
        ("── SCALP OVERVIEW ──────────────────", ""),
        ("Total Logged",       len(scalp_log)),
        ("Open",               sum(1 for s in scalp_log if s.get("status")=="OPEN")),
        ("Closed",             len(scalp_closed)),
        ("Wins",               len(scalp_wins)),
        ("Losses",             len(scalp_losses)),
        ("Win Rate",           f"{scalp_wr}%"),
        ("Total PnL",          f"{round(sum(scalp_pnls),2):+.2f}%" if scalp_pnls else "0%"),
        ("Total Blocked",      len(scalp_blocked)),
        ("", ""),
        ("── SCALP FILTER SETTINGS ──────────", ""),
        ("Min Confidence",     f"{SCALP_MIN_CONFIDENCE}%"),
        ("Min Score",          f"{SCALP_MIN_SCORE}/16"),
        ("Min Volume",         f"{SCALP_MIN_VOL_RATIO}x"),
        ("Min ADX",            f"{SCALP_ADX_MIN}"),
        ("TF Alignment",       "All 3 (1m+5m+15m)"),
        ("SL Multiplier",      "ATR × 1.0"),
        ("TP Multiplier",      "ATR × 2.0"),
        ("R:R Target",         "1:2"),
        ("Expiry",             "4 hours"),
        ("", ""),
        ("── VS SWING ───────────────────────", ""),
        ("Swing Min Confidence", f"{MIN_CONFIDENCE}%"),
        ("Swing Min Score",      f"{MIN_SCORE}/16"),
        ("Swing Min Volume",     f"{MIN_VOL_RATIO}x"),
        ("Swing Min ADX",        f"{ADX_MIN}"),
        ("Swing TF",             "15m+1h+4h"),
        ("Swing TP Multiplier",  "ATR × 4.0"),
        ("Swing Expiry",         "24 hours"),
    ]
    for r,(lbl,val) in enumerate(scalp_stats, start=2):
        sec = lbl.startswith("──")
        lf  = subhdr if sec else neu
        ws10.write(r, 0, lbl, lf)
        if val != "": ws10.write(r, 1, str(val), neu)

    wb.close()
    buf.seek(0)
    return buf.read()


# ── Background tasks ─────────────────────────────────────────────────
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

async def background_scanner():
    await asyncio.sleep(10)
    while True:
        # ── Swing scan ────────────────────────────────────────────────
        for symbol in PAIRS:
            try:
                sig = compute_signal(symbol)
                signal         = sig.get("signal", "HOLD")
                pre_block      = sig.get("pre_block_signal", "HOLD")
                news_blocked   = sig.get("news_blocked", False)
                regime_blocked = sig.get("regime_blocked", False)
                bias_blocked   = sig.get("bias_blocked", False)

                if signal != "HOLD":
                    grade = grade_signal(sig)
                    passed, reasons = passes_filters_detail(sig)
                    if passed:
                        log_signal_entry(sig, force_grade=grade)
                    else:
                        log_blocked_signal(sig, reasons)
                        log_signal_entry(sig, force_grade=grade)
                elif pre_block != "HOLD":
                    pseudo_sig = {**sig, "signal": pre_block}
                    grade = grade_signal(pseudo_sig)
                    reasons = []
                    if news_blocked:
                        reasons.append(f"News block: {sig.get('news_reason', 'high-impact event')}")
                    if regime_blocked:
                        reasons.append(f"Market regime blocked: {sig.get('regime_reason', 'UNKNOWN')}")
                    if bias_blocked:
                        reasons.append(f"Daily bias {sig.get('daily_bias','?')} blocks {pre_block}")
                    if reasons:
                        log_blocked_signal(pseudo_sig, reasons)
                        log_signal_entry(pseudo_sig, force_grade=grade)
            except Exception as e:
                print(f"[Swing Scanner] {symbol} error: {e}")
            await asyncio.sleep(3)

        # ── Scalp scan ────────────────────────────────────────────────
        for symbol in PAIRS:
            try:
                scalp_sig  = compute_scalp_signal(symbol)
                raw_signal = scalp_sig.get("signal", "HOLD")
                pre_block  = scalp_sig.get("pre_block_signal", "HOLD")

                if raw_signal != "HOLD":
                    # Grade the signal using same A/B/C logic as swing
                    grade = grade_signal(scalp_sig)
                    passed, reasons = passes_scalp_filters(scalp_sig)
                    # Log ALL directional signals to scalp_log for grade journal
                    # (same approach as swing — blocked signals still get logged
                    #  so A/B/C grade journal has data to compare)
                    log_scalp_signal(scalp_sig, passed, reasons, force_grade=grade)

                elif pre_block != "HOLD":
                    # Upstream blocked (news/regime/S/R) — log as blocked scalp
                    reasons = []
                    if scalp_sig.get("news_blocked"):
                        reasons.append(f"News block: {scalp_sig.get('news_risk_level','?')}")
                    if scalp_sig.get("regime_blocked"):
                        reasons.append("Market regime blocked")
                    if scalp_sig.get("sr_blocked"):
                        reasons.append("S/R block: price too close to S/R level")
                    if reasons:
                        pseudo = {**scalp_sig, "signal": pre_block}
                        grade  = grade_signal(pseudo)
                        log_scalp_signal(pseudo, False, reasons, force_grade=grade)

            except Exception as e:
                print(f"[Scalp Scanner] {symbol} error: {e}")
            await asyncio.sleep(2)

        try:
            update_signal_outcomes()
            update_blocked_outcomes()
            update_scalp_outcomes()
        except Exception as e:
            print(f"[Outcome updater] {e}")
        await asyncio.sleep(300)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_scanner())


# ── Endpoints ─────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "Bot running ✅", "version": "7.0.0"}

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


@app.get("/scalp/signal/{symbol}")
def get_scalp_signal(symbol: str = "BTCUSDT"):
    try:
        return compute_scalp_signal(symbol)
    except Exception as e:
        import traceback
        return {"error": repr(e), "trace": traceback.format_exc()}


@app.get("/scalp/log")
def get_scalp_log(limit: int = 50):
    update_scalp_outcomes()
    return {"total": len(scalp_log), "signals": scalp_log[-limit:]}


@app.get("/scalp/blocked")
def get_scalp_blocked(limit: int = 50):
    return {"total": len(scalp_blocked), "signals": scalp_blocked[-limit:]}


@app.get("/scalp/stats")
def get_scalp_stats():
    update_scalp_outcomes()
    closed  = [s for s in scalp_log if s.get("status") == "CLOSED"]
    wins    = [s for s in closed if s.get("outcome") == "WIN"]
    losses  = [s for s in closed if s.get("outcome") == "LOSS"]
    pnls    = [s["pnl_pct"] for s in closed if s.get("pnl_pct") is not None]
    wr      = round(len(wins) / max(len(wins)+len(losses), 1) * 100, 1)
    blocked_total = len(scalp_blocked)
    by_filter = {}
    for f in ["confidence","score","volume","adx","tf_alignment","news","market_regime"]:
        bf = [b for b in scalp_blocked if b.get("blocked_by") == f]
        if bf: by_filter[f] = len(bf)
    return {
        "mode":           "SCALP",
        "total_logged":   len(scalp_log),
        "open":           sum(1 for s in scalp_log if s.get("status") == "OPEN"),
        "closed":         len(closed),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       f"{wr}%",
        "total_pnl":      f"{round(sum(pnls), 2):+.2f}%" if pnls else "0%",
        "total_blocked":  blocked_total,
        "blocked_by_filter": by_filter,
        "last_updated":   datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


@app.get("/scalp/scan")
def scalp_scan_all():
    results = []
    for symbol in PAIRS:
        try:
            sig = compute_scalp_signal(symbol)
            results.append({
                "symbol":     symbol,
                "signal":     sig["signal"],
                "confidence": sig["confidence"],
                "score":      sig["score"],
                "adx":        sig.get("adx"),
                "vol_ratio":  sig.get("vol_ratio"),
                "timeframes": sig.get("timeframes"),
                "risk_reward": sig.get("risk_reward"),
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})
    return {"mode": "SCALP", "pairs": results}


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


# ══════════════════════════════════════════════════════════════════════
# NEW: Signal tracker endpoints
# ══════════════════════════════════════════════════════════════════════

@app.get("/signals/log")
def get_signal_log():
    """Returns all logged signals as JSON."""
    update_signal_outcomes()
    total   = len(signal_log)
    wins    = sum(1 for s in signal_log if s["outcome"] == "WIN")
    losses  = sum(1 for s in signal_log if s["outcome"] == "LOSS")
    open_ct = sum(1 for s in signal_log if s["status"] == "OPEN")
    return {
        "total":   total,
        "wins":    wins,
        "losses":  losses,
        "open":    open_ct,
        "win_rate": f"{round(wins/max(wins+losses,1)*100,1)}%",
        "signals": signal_log[-50:],  # last 50
    }

@app.get("/signals/stats")
def get_signal_stats():
    """Quick stats for Android app dashboard."""
    update_signal_outcomes()
    closed = [s for s in signal_log if s["status"] == "CLOSED"]
    wins   = sum(1 for s in closed if s["outcome"] == "WIN")
    losses = sum(1 for s in closed if s["outcome"] == "LOSS")
    tpnl   = round(sum(s["pnl_pct"] for s in closed if s["pnl_pct"] is not None), 2)
    return {
        "total_logged":  len(signal_log),
        "open":          sum(1 for s in signal_log if s["status"] == "OPEN"),
        "closed":        len(closed),
        "wins":          wins,
        "losses":        losses,
        "win_rate":      f"{round(wins/max(wins+losses,1)*100,1)}%",
        "total_pnl":     f"{tpnl}%",
        "last_updated":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

@app.delete("/signals/clear")
def clear_signal_log():
    """Clear all logged signals (use carefully)."""
    global signal_log
    signal_log = []
    save_signal_log(signal_log)
    return {"status": "Signal log cleared"}


# ══════════════════════════════════════════════════════════════════════
# TRADE JOURNAL — Grade Performance Analysis
# ══════════════════════════════════════════════════════════════════════

def _grade_stats(signals: list, grade: str) -> dict:
    """Compute win rate and avg profit for a single grade bucket."""
    bucket = [s for s in signals if s.get("grade") == grade]
    closed = [s for s in bucket if s["status"] == "CLOSED"]
    wins   = [s for s in closed if s["outcome"] == "WIN"]
    losses = [s for s in closed if s["outcome"] == "LOSS"]
    pnls   = [s["pnl_pct"] for s in closed if s["pnl_pct"] is not None]

    win_rate   = round(len(wins) / max(len(wins) + len(losses), 1) * 100, 1)
    avg_profit = round(sum(pnls) / len(pnls), 2) if pnls else 0
    avg_win    = round(sum(s["pnl_pct"] for s in wins if s["pnl_pct"] is not None)
                       / max(len(wins), 1), 2) if wins else 0
    avg_loss   = round(sum(s["pnl_pct"] for s in losses if s["pnl_pct"] is not None)
                       / max(len(losses), 1), 2) if losses else 0

    return {
        "grade":        grade,
        "total":        len(bucket),
        "open":         len(bucket) - len(closed),
        "closed":       len(closed),
        "wins":         len(wins),
        "losses":       len(losses),
        "expired":      sum(1 for s in closed if s["outcome"] == "EXPIRED"),
        "win_rate":     f"{win_rate}%",
        "avg_profit":   f"{avg_profit:+.2f}%",
        "avg_win":      f"{avg_win:+.2f}%",
        "avg_loss":     f"{avg_loss:+.2f}%",
        "total_pnl":    f"{round(sum(pnls), 2):+.2f}%",
        # Snapshot averages — how the trade looked at 1h/4h/24h
        "avg_pnl_1h":   f"{round(sum(s['pnl_1h'] for s in bucket if s.get('pnl_1h') is not None) / max(sum(1 for s in bucket if s.get('pnl_1h') is not None), 1), 2):+.2f}%",
        "avg_pnl_4h":   f"{round(sum(s['pnl_4h'] for s in bucket if s.get('pnl_4h') is not None) / max(sum(1 for s in bucket if s.get('pnl_4h') is not None), 1), 2):+.2f}%",
        "avg_pnl_24h":  f"{round(sum(s['pnl_24h'] for s in bucket if s.get('pnl_24h') is not None) / max(sum(1 for s in bucket if s.get('pnl_24h') is not None), 1), 2):+.2f}%",
        "avg_max_profit":   f"{round(sum(s['max_profit_pct'] for s in bucket if s.get('max_profit_pct') is not None) / max(sum(1 for s in bucket if s.get('max_profit_pct') is not None), 1), 2):+.2f}%",
        "avg_max_drawdown": f"{round(sum(s['max_drawdown_pct'] for s in bucket if s.get('max_drawdown_pct') is not None) / max(sum(1 for s in bucket if s.get('max_drawdown_pct') is not None), 1), 2):+.2f}%",
        "thresholds": (
            "Conf≥70 Score≥11 ADX≥25 Vol≥1.0x All 3TF" if grade == "A" else
            "Conf≥55 Score≥9  ADX≥20 Vol≥0.8x 2/3 TF"  if grade == "B" else
            "Everything else (directional signals only)"
        ),
    }


@app.get("/journal/grade-stats")
def journal_grade_stats():
    """
    Returns A/B/C grade performance comparison.
    After 50-100 signals this tells you which filter tier is most profitable.
    """
    update_signal_outcomes()
    directional = [s for s in signal_log if s.get("signal") in ("BUY", "SELL")]
    return {
        "total_signals":     len(directional),
        "summary":           (
            f"{len(directional)} directional signals logged — "
            f"{sum(1 for s in directional if s.get('grade')=='A')} A-grade, "
            f"{sum(1 for s in directional if s.get('grade')=='B')} B-grade, "
            f"{sum(1 for s in directional if s.get('grade')=='C')} C-grade"
        ),
        "grades": {
            "A": _grade_stats(directional, "A"),
            "B": _grade_stats(directional, "B"),
            "C": _grade_stats(directional, "C"),
        },
        "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


@app.get("/journal/signals")
def journal_signals(grade: str = None, limit: int = 100):
    """
    Returns the raw signal journal, optionally filtered by grade (A/B/C).
    Includes all filter values at signal time + outcome snapshots.
    GET /journal/signals?grade=B&limit=50
    """
    update_signal_outcomes()
    sigs = signal_log
    if grade:
        sigs = [s for s in sigs if s.get("grade", "").upper() == grade.upper()]
    return {
        "count":   len(sigs),
        "signals": sigs[-limit:],
    }


# ══════════════════════════════════════════════════════════════════════
# BLOCKED SIGNALS — Which filters are rejecting profitable trades?
# ══════════════════════════════════════════════════════════════════════

@app.get("/blocked/log")
def get_blocked_log(limit: int = 50):
    """Returns the most recent blocked signals with reasons."""
    update_blocked_outcomes()
    return {
        "total_blocked": len(blocked_log),
        "signals":       blocked_log[-limit:],
    }


@app.get("/blocked/stats")
def get_blocked_stats():
    """
    The key analysis endpoint.
    For each filter, shows:
      - How many signals it blocked
      - Of those, how many would have WON (based on 24h price action)
      - Win rate of signals it blocked
    After 50+ blocked signals this answers: 'which filter is costing me money?'
    """
    update_blocked_outcomes()

    if not blocked_log:
        return {"message": "No blocked signals yet — scanner will populate this over time."}

    # ── Per-filter breakdown ──────────────────────────────────────────
    filters = ["confidence", "score", "volume", "adx", "tf_alignment",
               "macd", "rsi", "outside_hours", "market_regime",
               "daily_bias", "orderbook", "news", "other"]

    stats = {}
    for f in filters:
        blocked_by_f = [b for b in blocked_log if b.get("blocked_by") == f]
        evaluated    = [b for b in blocked_by_f if b.get("would_have_won") is not None]
        would_win    = [b for b in evaluated if b.get("would_have_won") is True]
        would_lose   = [b for b in evaluated if b.get("would_have_won") is False]

        if not blocked_by_f:
            continue

        avg_move_1h  = _avg(b.get("move_1h_pct") for b in blocked_by_f if b.get("move_1h_pct") is not None)
        avg_move_24h = _avg(b.get("move_24h_pct") for b in blocked_by_f if b.get("move_24h_pct") is not None)

        stats[f] = {
            "filter":            f,
            "total_blocked":     len(blocked_by_f),
            "evaluated":         len(evaluated),
            "would_have_won":    len(would_win),
            "would_have_lost":   len(would_lose),
            "missed_win_rate":   f"{round(len(would_win) / max(len(evaluated), 1) * 100, 1)}%",
            "avg_move_1h":       f"{avg_move_1h:+.2f}%" if avg_move_1h is not None else "n/a",
            "avg_move_24h":      f"{avg_move_24h:+.2f}%" if avg_move_24h is not None else "n/a",
            "verdict":           _filter_verdict(f, len(would_win), len(evaluated)),
        }

    # ── Overall summary ───────────────────────────────────────────────
    evaluated_all  = [b for b in blocked_log if b.get("would_have_won") is not None]
    missed_wins    = sum(1 for b in evaluated_all if b["would_have_won"])
    total_blocked  = len(blocked_log)

    return {
        "total_blocked":   total_blocked,
        "evaluated":       len(evaluated_all),
        "missed_wins":     missed_wins,
        "missed_win_rate": f"{round(missed_wins / max(len(evaluated_all), 1) * 100, 1)}%",
        "summary": (
            f"Of {len(evaluated_all)} evaluated blocked signals, "
            f"{missed_wins} ({round(missed_wins/max(len(evaluated_all),1)*100,1)}%) "
            f"would have been winners. Filters with high missed-win-rate are candidates to relax."
        ),
        "by_filter":       stats,
        "last_updated":    datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def _avg(values) -> float | None:
    vals = list(values)
    return round(sum(vals) / len(vals), 2) if vals else None


def _filter_verdict(filter_name: str, would_win: int, evaluated: int) -> str:
    if evaluated < 5:
        return "⏳ Not enough data yet"
    rate = would_win / evaluated
    if rate >= 0.6:
        return f"⚠️ HIGH missed-win rate ({rate*100:.0f}%) — consider relaxing this filter"
    if rate >= 0.45:
        return f"🟡 Moderate missed-win rate ({rate*100:.0f}%) — worth monitoring"
    return f"✅ Blocking correctly ({rate*100:.0f}% would have won — good filter)"


@app.delete("/blocked/clear")
def clear_blocked_log():
    """Clear blocked signals log (use carefully)."""
    global blocked_log
    blocked_log = []
    save_blocked_log(blocked_log)
    return {"status": "Blocked signals log cleared"}




# ══════════════════════════════════════════════════════════════════════
# NEW: Excel download endpoint — call from Android app
# ══════════════════════════════════════════════════════════════════════

@app.get("/signals/download")
def download_signal_report():
    """
    Swing signal report — All Signals, Open, Daily Summary, By Symbol,
    Dashboard, Blocked Signals, Grade Analysis sheets.
    """
    try:
        excel_bytes = generate_excel_bytes(mode="swing")
        filename    = f"swing_report_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.xlsx"
        return StreamingResponse(
            io.BytesIO(excel_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except ImportError:
        raise HTTPException(status_code=500, detail="xlsxwriter not installed. Run: pip install xlsxwriter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/scalp/download")
def download_scalp_report():
    """
    Scalp signal report — Scalp Signals, Scalp Blocked, Scalp Stats sheets only.
    """
    try:
        excel_bytes = generate_excel_bytes(mode="scalp")
        filename    = f"scalp_report_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.xlsx"
        return StreamingResponse(
            io.BytesIO(excel_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except ImportError:
        raise HTTPException(status_code=500, detail="xlsxwriter not installed. Run: pip install xlsxwriter")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Trade monitor endpoints ───────────────────────────────────────────

@app.post("/monitor/start")
async def start_monitor(
    symbol: str, direction: str, entry_price: float,
    stop_loss: float, take_profit: float, market: str = "spot"
):
    try:
        if symbol in active_trades:
            raise HTTPException(status_code=400, detail=f"{symbol} already monitored.")
        if entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
            raise HTTPException(status_code=400, detail="Invalid SL/TP/entry values.")
        if direction == "BUY" and stop_loss >= entry_price:
            raise HTTPException(status_code=400, detail="BUY: stop_loss must be below entry.")
        if direction == "SELL" and stop_loss <= entry_price:
            raise HTTPException(status_code=400, detail="SELL: stop_loss must be above entry.")
        if direction not in ("BUY", "SELL"):
            raise HTTPException(status_code=400, detail="direction must be BUY or SELL.")

        from monitor.trade_monitor import OpenTrade, monitor_trade
        trade = OpenTrade(
            symbol=symbol, direction=direction,
            entry_price=entry_price, stop_loss=stop_loss,
            take_profit=take_profit, market=market
        )
        active_trades[symbol] = trade
        asyncio.create_task(monitor_trade(trade))
        return {"status": "Monitoring started ✅", "symbol": symbol,
                "direction": direction, "entry_price": entry_price,
                "stop_loss": stop_loss, "take_profit": take_profit}
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}

@app.get("/monitor/active")
def get_active_trades():
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
    win_rate  = round(len(wins) / len(history) * 100, 1)
    avg_win   = round(sum(t["pnl_pct"] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss  = round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0
    return {
        "total_trades":  len(history),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      f"{win_rate}%",
        "total_pnl":     f"{round(total_pnl,2)}%",
        "avg_win":       f"{avg_win}%",
        "avg_loss":      f"{avg_loss}%",
        "daily_pnl":     f"{auto_trader.daily_pnl:.2f}%",
        "recent_trades": history[-5:],
    }


@app.get("/news")
def get_news_status(symbol: str = "BTCUSDT"):
    """
    Check current news safety for a symbol.
    GET /news?symbol=BTCUSDT
    Returns sentiment, upcoming events, risk level.
    """
    from news_filter import check_news_safety, get_upcoming_events
    try:
        info   = check_news_safety(symbol.upper())
        events = get_upcoming_events(within_minutes=240)  # next 4 hours
        return {
            "symbol":       symbol.upper(),
            "safe":         info["safe"],
            "risk_level":   info["risk_level"],
            "reason":       info["reason"],
            "sentiment":    info["sentiment"],
            "news_score":   info["news_score"],
            "risk_news":    info["risk_news"],
            "upcoming_events": [
                {
                    "title":        e["title"],
                    "minutes_away": e["minutes_away"],
                    "in_blackout":  e["in_blackout"],
                }
                for e in events[:5]
            ],
            "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/news/scan")
def scan_news_all():
    """Check news safety for all 5 pairs at once."""
    from news_filter import check_news_safety
    results = {}
    for sym in PAIRS:
        try:
            info = check_news_safety(sym)
            results[sym] = {
                "safe":       info["safe"],
                "risk_level": info["risk_level"],
                "sentiment":  info["sentiment"],
                "score":      info["news_score"],
                "reason":     info["reason"],
            }
        except Exception as e:
            results[sym] = {"error": str(e)}
    blocked = [s for s, r in results.items() if not r.get("safe", True)]
    return {
        "summary":  f"{len(blocked)} pairs blocked by news" if blocked else "All pairs clear",
        "blocked":  blocked,
        "results":  results,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


# ══════════════════════════════════════════════════════════════════════
# BREAKOUT SCAN ENDPOINT
# GET /breakout/scan — scans all pairs for Phase 1 or Phase 2 setups
# GET /breakout/scan?symbol=BTCUSDT — single pair
# ══════════════════════════════════════════════════════════════════════

@app.get("/breakout/scan")
def breakout_scan(symbol: str = None):
    """
    Scans for breakout setups across all pairs (or one specific pair).
    Phase 1 = Compression building (watch alert)
    Phase 2 = Confirmed breakout (trade signal)
    """
    from strategy.indicators import prepare_dataframe, add_indicators, detect_breakout

    targets = [symbol.upper()] if symbol else PAIRS
    results = []

    for sym in targets:
        try:
            spot       = get_spot()
            klines_1h  = spot.get_klines(sym, interval="1h",  limit=200)
            klines_4h  = spot.get_klines(sym, interval="4h",  limit=60)
            df_1h      = add_indicators(prepare_dataframe(klines_1h))
            df_4h      = add_indicators(prepare_dataframe(klines_4h))
            price      = float(df_1h["close"].iloc[-1])
            bo         = detect_breakout(df_1h, df_4h)

            results.append({
                "symbol":             sym,
                "price":              round(price, 4),
                "breakout_phase":     bo["phase"],
                "breakout_status":    bo["status"],
                "breakout_direction": bo["direction"],
                "watch_level":        bo.get("watch_level") or bo.get("breakout_level"),
                "measured_target":    bo.get("measured_target"),
                "move_pct":           bo.get("move_pct", 0),
                "strength":           bo.get("strength", "NONE"),
                "vol_ratio":          bo.get("vol_ratio", 0),
                "bb_width":           bo.get("bb_width", 0),
                "adx":                bo.get("adx", 0),
                "message":            bo["message"],
            })
        except Exception as e:
            results.append({"symbol": sym, "error": str(e)})

    # Sort: Phase 2 first, then Phase 1 by strength, then no setup
    def sort_key(r):
        phase = r.get("breakout_phase", 0)
        strength_order = {"STRONG": 0, "MODERATE": 1, "BUILDING": 2, "NONE": 3}
        s = strength_order.get(r.get("strength", "NONE"), 3)
        return (-phase, s)

    results.sort(key=sort_key)

    phase2 = [r for r in results if r.get("breakout_phase") == 2]
    phase1 = [r for r in results if r.get("breakout_phase") == 1]

    return {
        "scanned":       len(results),
        "phase2_count":  len(phase2),
        "phase1_count":  len(phase1),
        "summary":       (
            f"{len(phase2)} confirmed breakout(s), {len(phase1)} compression setup(s)"
            if phase2 or phase1 else "No breakout setups detected across all pairs"
        ),
        "results":       results,
    }
