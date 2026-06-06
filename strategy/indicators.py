import pandas as pd
import ta

def prepare_dataframe(klines) -> pd.DataFrame:
    if isinstance(klines, dict):
        klines = klines.get("data", klines.get("result", []))
    if not klines or len(klines) == 0:
        raise ValueError("No kline data returned")
    first = klines[0]
    if isinstance(first, dict):
        df = pd.DataFrame(klines)
        df = df.rename(columns={
            "t": "time", "o": "open", "h": "high",
            "l": "low",  "c": "close", "v": "volume"
        })
    elif len(first) == 8:
        df = pd.DataFrame(klines, columns=[
            "time","open","high","low",
            "close","volume","close_time","quote_volume"
        ])
    else:
        df = pd.DataFrame(klines, columns=[
            "time","open","high","low","close","volume",
            "close_time","quote_volume","trades",
            "taker_buy_base","taker_buy_quote","ignore"
        ])
    for col in ["open","high","low","close","volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"], unit="ms")
        df.set_index("time", inplace=True)
    return df

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # EMA stack
    df["ema9"]   = ta.trend.ema_indicator(df["close"], window=9)
    df["ema21"]  = ta.trend.ema_indicator(df["close"], window=21)
    df["ema50"]  = ta.trend.ema_indicator(df["close"], window=50)
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)

    # RSI
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    # Stochastic RSI
    stoch = ta.momentum.StochRSIIndicator(df["close"], window=14)
    df["stoch_rsi_k"] = stoch.stochrsi_k()
    df["stoch_rsi_d"] = stoch.stochrsi_d()

    # MACD
    macd = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd"]        = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"]   = macd.macd_diff()

    # Bollinger Bands
    bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_mid"]   = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = bb.bollinger_wband()

    # ATR
    df["atr"] = ta.volatility.average_true_range(
        df["high"], df["low"], df["close"], window=14
    )

    # ADX
    df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)

    # VWAP (intraday volume weighted price)
    df["vwap"] = ta.volume.volume_weighted_average_price(
        df["high"], df["low"], df["close"], df["volume"]
    )

    # Volume SMA (is current volume above average?)
    df["vol_sma"] = df["volume"].rolling(window=20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_sma"]

    return df

def generate_signal(df: pd.DataFrame) -> dict:
    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    price = float(last["close"])

    signal  = "HOLD"
    reasons = []
    score   = 0

    # ── Trend Filter (EMA200) ──────────────────────
    above_ema200 = price > float(last["ema200"]) if pd.notna(last["ema200"]) else True
    below_ema200 = price < float(last["ema200"]) if pd.notna(last["ema200"]) else True

    # ── EMA Crossover ─────────────────────────────
    ema_cross_up   = prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"]
    ema_cross_down = prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"]
    ema_bull_stack = last["ema9"] > last["ema21"] > last["ema50"]
    ema_bear_stack = last["ema9"] < last["ema21"] < last["ema50"]

    # ── RSI ───────────────────────────────────────
    rsi = float(last["rsi"])
    rsi_buy  = 35 < rsi < 65
    rsi_sell = 35 < rsi < 65
    rsi_oversold  = rsi < 35
    rsi_overbought = rsi > 65

    # ── Stochastic RSI ────────────────────────────
    stoch_k = float(last["stoch_rsi_k"]) if pd.notna(last["stoch_rsi_k"]) else 0.5
    stoch_cross_up   = (prev["stoch_rsi_k"] < prev["stoch_rsi_d"] and
                        last["stoch_rsi_k"] > last["stoch_rsi_d"])
    stoch_cross_down = (prev["stoch_rsi_k"] > prev["stoch_rsi_d"] and
                        last["stoch_rsi_k"] < last["stoch_rsi_d"])

    # ── MACD ──────────────────────────────────────
    macd_bullish = last["macd"] > last["macd_signal"] and last["macd_hist"] > 0
    macd_bearish = last["macd"] < last["macd_signal"] and last["macd_hist"] < 0
    macd_cross_up   = prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]
    macd_cross_down = prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]

    # ── VWAP ──────────────────────────────────────
    vwap = float(last["vwap"]) if pd.notna(last["vwap"]) else price
    above_vwap = price > vwap
    below_vwap = price < vwap

    # ── Volume ────────────────────────────────────
    vol_ratio = float(last["vol_ratio"]) if pd.notna(last["vol_ratio"]) else 1.0
    high_volume = vol_ratio > 1.2

    # ── ADX (trend strength) ──────────────────────
    adx = float(last["adx"]) if pd.notna(last["adx"]) else 0
    strong_trend = adx > 25

    # ── Bollinger Band Position ───────────────────
    bb_squeeze = float(last["bb_width"]) < 0.02 if pd.notna(last["bb_width"]) else False
    near_lower = price <= last["bb_mid"]
    near_upper = price >= last["bb_mid"]

    # ── BUY Score ─────────────────────────────────
    buy_score = 0
    buy_reasons = []

    if ema_cross_up or ema_bull_stack:
        buy_score += 2
        buy_reasons.append("EMA bullish stack/cross")
    if macd_bullish or macd_cross_up:
        buy_score += 2
        buy_reasons.append("MACD bullish")
    if rsi_buy or rsi_oversold:
        buy_score += 1
        buy_reasons.append(f"RSI {rsi:.1f} favorable")
    if stoch_cross_up or stoch_k < 0.3:
        buy_score += 2
        buy_reasons.append("Stoch RSI bullish")
    if above_vwap:
        buy_score += 1
        buy_reasons.append("Price above VWAP")
    if high_volume:
        buy_score += 1
        buy_reasons.append(f"High volume {vol_ratio:.1f}x")
    if strong_trend:
        buy_score += 1
        buy_reasons.append(f"Strong trend ADX {adx:.0f}")
    if above_ema200:
        buy_score += 1
        buy_reasons.append("Above EMA200 — bull market")
    if near_lower:
        buy_score += 1
        buy_reasons.append("Price at BB support")

    # ── SELL Score ────────────────────────────────
    sell_score = 0
    sell_reasons = []

    if ema_cross_down or ema_bear_stack:
        sell_score += 2
        sell_reasons.append("EMA bearish stack/cross")
    if macd_bearish or macd_cross_down:
        sell_score += 2
        sell_reasons.append("MACD bearish")
    if rsi_sell or rsi_overbought:
        sell_score += 1
        sell_reasons.append(f"RSI {rsi:.1f} favorable")
    if stoch_cross_down or stoch_k > 0.7:
        sell_score += 2
        sell_reasons.append("Stoch RSI bearish")
    if below_vwap:
        sell_score += 1
        sell_reasons.append("Price below VWAP")
    if high_volume:
        sell_score += 1
        sell_reasons.append(f"High volume {vol_ratio:.1f}x")
    if strong_trend:
        sell_score += 1
        sell_reasons.append(f"Strong trend ADX {adx:.0f}")
    if below_ema200:
        sell_score += 1
        sell_reasons.append("Below EMA200 — bear market")
    if near_upper:
        sell_score += 1
        sell_reasons.append("Price at BB resistance")

    # ── Signal Decision (need score >= 6 out of 12) ──
    if buy_score >= 6 and buy_score > sell_score:
        signal  = "BUY"
        reasons = buy_reasons
        score   = buy_score
    elif sell_score >= 6 and sell_score > buy_score:
        signal  = "SELL"
        reasons = sell_reasons
        score   = sell_score
    else:
        signal  = "HOLD"
        reasons = ["Score too low — waiting for clearer signal"]
        score   = max(buy_score, sell_score)

    confidence = f"{int((score/12)*100)}%"

    # ── Risk Management ───────────────────────────
    atr_val = float(last["atr"]) if pd.notna(last["atr"]) else price * 0.01

    if signal == "BUY":
        stop_loss   = round(price - (atr_val * 1.5), 2)
        take_profit = round(price + (atr_val * 3.0), 2)
    elif signal == "SELL":
        stop_loss   = round(price + (atr_val * 1.5), 2)
        take_profit = round(price - (atr_val * 3.0), 2)
    else:
        stop_loss   = round(price - (atr_val * 1.5), 2)
        take_profit = round(price + (atr_val * 3.0), 2)

    risk        = abs(price - stop_loss)
    reward      = abs(take_profit - price)
    risk_reward = f"1:{round(reward/risk, 1)}" if risk > 0 else "1:2"

    # Position size (1.5% risk of $1000 account)
    position_size = round(1000 * 0.015 / risk, 4) if risk > 0 else 0

    return {
        "signal":        signal,
        "confidence":    confidence,
        "score":         f"{score}/12",
        "price":         round(price, 4),
        "rsi":           round(rsi, 2),
        "stoch_k":       round(stoch_k * 100, 2),
        "macd_hist":     round(float(last["macd_hist"]), 6),
        "ema9":          round(float(last["ema9"]), 4),
        "ema21":         round(float(last["ema21"]), 4),
        "ema50":         round(float(last["ema50"]), 4),
        "ema200":        round(float(last["ema200"]), 4) if pd.notna(last["ema200"]) else 0,
        "vwap":          round(vwap, 4),
        "bb_lower":      round(float(last["bb_lower"]), 4),
        "bb_upper":      round(float(last["bb_upper"]), 4),
        "atr":           round(atr_val, 4),
        "adx":           round(adx, 2),
        "vol_ratio":     round(vol_ratio, 2),
        "above_ema200":  above_ema200,
        "high_volume":   high_volume,
        "strong_trend":  strong_trend,
        "stop_loss":     stop_loss,
        "take_profit":   take_profit,
        "position_size": position_size,
        "risk_reward":   risk_reward,
        "reasons":       reasons
    }