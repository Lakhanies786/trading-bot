import pandas as pd
import pandas_ta as ta 

def prepare_dataframe(klines: list) -> pd.DataFrame:
    if len(klines[0]) == 8:
        df = pd.DataFrame(klines, columns=[
            "time", "open", "high", "low",
            "close", "volume", "close_time", "quote_volume"
        ])
    else:
        df = pd.DataFrame(klines, columns=[
            "time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore"
        ])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col])
    df["time"] = pd.to_datetime(df["time"], unit="ms")
    df.set_index("time", inplace=True)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # ── EMA Cross ──────────────────────────────────────────
    df["ema9"]  = ta.trend.ema_indicator(df["close"], window=9)
    df["ema21"] = ta.trend.ema_indicator(df["close"], window=21)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)

    # ── RSI ────────────────────────────────────────────────
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    # ── MACD ───────────────────────────────────────────────
    macd = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd"]        = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"]   = macd.macd_diff()

    # ── Bollinger Bands ────────────────────────────────────
    bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_mid"]   = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()

    # ── ATR ────────────────────────────────────────────────
    df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], window=14)

    # ── Volume Analysis ────────────────────────────────────
    df["vol_sma"]     = df["volume"].rolling(window=20).mean()
    df["high_volume"] = df["volume"] > (df["vol_sma"] * 1.5)

    # ── RSI Divergence ────────────────────────────────────
    df["rsi_prev"]    = df["rsi"].shift(1)
    df["price_prev"]  = df["close"].shift(1)
    df["bearish_div"] = (df["close"] > df["price_prev"]) & (df["rsi"] < df["rsi_prev"])
    df["bullish_div"] = (df["close"] < df["price_prev"]) & (df["rsi"] > df["rsi_prev"])

    # ── Candlestick Patterns ───────────────────────────────
    df["body"]        = abs(df["close"] - df["open"])
    df["upper_wick"]  = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_wick"]  = df[["close", "open"]].min(axis=1) - df["low"]
    df["hammer"]      = (df["lower_wick"] > df["body"] * 2) & (df["upper_wick"] < df["body"] * 0.5)
    df["shooting_star"] = (df["upper_wick"] > df["body"] * 2) & (df["lower_wick"] < df["body"] * 0.5)
    df["doji"]        = df["body"] < (df["high"] - df["low"]) * 0.1

    # ── ADX Trend Strength ─────────────────────────────────
    df["adx"]          = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["strong_trend"] = df["adx"] > 25

    return df


def calculate_risk_management(price: float, signal: str, atr: float) -> dict:
    atr_multiplier_sl = 1.5
    atr_multiplier_tp = 3.0

    if signal == "BUY":
        stop_loss   = round(price - (atr * atr_multiplier_sl), 4)
        take_profit = round(price + (atr * atr_multiplier_tp), 4)
    elif signal == "SELL":
        stop_loss   = round(price + (atr * atr_multiplier_sl), 4)
        take_profit = round(price - (atr * atr_multiplier_tp), 4)
    else:
        stop_loss   = None
        take_profit = None

    account_size  = 1000
    risk_percent  = 0.02
    risk_amount   = account_size * risk_percent
    if stop_loss and price:
        sl_distance   = abs(price - stop_loss)
        position_size = round(risk_amount / sl_distance, 6) if sl_distance > 0 else 0
    else:
        position_size = 0

    return {
        "stop_loss":     stop_loss,
        "take_profit":   take_profit,
        "position_size": position_size,
        "risk_amount":   risk_amount,
        "risk_reward":   "1:2"
    }


def generate_signal(df: pd.DataFrame) -> dict:
    last  = df.iloc[-1]
    prev  = df.iloc[-2]

    signal  = "HOLD"
    reasons = []
    score   = 0

    ema_cross_up   = prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"]
    ema_cross_down = prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"]
    above_ema50    = last["close"] > last["ema50"]

    rsi_ok_buy     = 40 < last["rsi"] < 70
    rsi_ok_sell    = 30 < last["rsi"] < 60
    rsi_oversold   = last["rsi"] < 35
    rsi_overbought = last["rsi"] > 65

    macd_bullish = last["macd"] > last["macd_signal"]
    macd_bearish = last["macd"] < last["macd_signal"]

    price           = last["close"]
    near_lower_band = price <= last["bb_mid"]
    near_upper_band = price >= last["bb_mid"]
    at_bb_lower     = price <= last["bb_lower"] * 1.005
    at_bb_upper     = price >= last["bb_upper"] * 0.995

    high_vol     = bool(last["high_volume"])
    strong_trend = bool(last["strong_trend"])
    hammer       = bool(last["hammer"])
    shooting_star = bool(last["shooting_star"])
    bullish_div  = bool(last["bullish_div"])
    bearish_div  = bool(last["bearish_div"])

    # ── BUY scoring ───────────────────────────────────────
    if ema_cross_up:
        score += 2; reasons.append("✅ EMA 9 crossed above EMA 21")
    if above_ema50:
        score += 1; reasons.append("✅ Price above EMA 50 (uptrend)")
    if rsi_ok_buy:
        score += 1; reasons.append(f"✅ RSI {last['rsi']:.1f} healthy buy range")
    if rsi_oversold:
        score += 2; reasons.append(f"✅ RSI {last['rsi']:.1f} oversold — strong buy")
    if macd_bullish:
        score += 1; reasons.append("✅ MACD bullish")
    if near_lower_band or at_bb_lower:
        score += 1; reasons.append("✅ Price near BB lower band")
    if high_vol and ema_cross_up:
        score += 2; reasons.append("✅ High volume confirms move")
    if strong_trend:
        score += 1; reasons.append("✅ Strong trend (ADX > 25)")
    if hammer:
        score += 2; reasons.append("✅ Hammer candle — bullish reversal")
    if bullish_div:
        score += 2; reasons.append("✅ RSI bullish divergence")

    if score >= 5 and (ema_cross_up or rsi_oversold or hammer):
        signal = "BUY"
    else:
        score = 0; reasons = []

        # ── SELL scoring ──────────────────────────────────
        if ema_cross_down:
            score += 2; reasons.append("✅ EMA 9 crossed below EMA 21")
        if not above_ema50:
            score += 1; reasons.append("✅ Price below EMA 50 (downtrend)")
        if rsi_ok_sell:
            score += 1; reasons.append(f"✅ RSI {last['rsi']:.1f} sell range")
        if rsi_overbought:
            score += 2; reasons.append(f"✅ RSI {last['rsi']:.1f} overbought — strong sell")
        if macd_bearish:
            score += 1; reasons.append("✅ MACD bearish")
        if near_upper_band or at_bb_upper:
            score += 1; reasons.append("✅ Price near BB upper band")
        if high_vol and ema_cross_down:
            score += 2; reasons.append("✅ High volume confirms move")
        if strong_trend:
            score += 1; reasons.append("✅ Strong trend (ADX > 25)")
        if shooting_star:
            score += 2; reasons.append("✅ Shooting star — bearish reversal")
        if bearish_div:
            score += 2; reasons.append("✅ RSI bearish divergence")

        if score >= 5 and (ema_cross_down or rsi_overbought or shooting_star):
            signal = "SELL"
        else:
            reasons = ["No strong signal conditions met yet"]

    max_score  = 15
    confidence = min(round((score / max_score) * 100), 100)
    atr        = last["atr"]
    risk       = calculate_risk_management(price, signal, atr)

    return {
        "signal":        signal,
        "confidence":    f"{confidence}%",
        "price":         round(price, 4),
        "rsi":           round(last["rsi"], 2),
        "macd_hist":     round(last["macd_hist"], 6),
        "ema9":          round(last["ema9"], 4),
        "ema21":         round(last["ema21"], 4),
        "ema50":         round(last["ema50"], 4),
        "bb_lower":      round(last["bb_lower"], 4),
        "bb_upper":      round(last["bb_upper"], 4),
        "atr":           round(atr, 4),
        "adx":           round(last["adx"], 2),
        "high_volume":   high_vol,
        "strong_trend":  strong_trend,
        "stop_loss":     risk["stop_loss"],
        "take_profit":   risk["take_profit"],
        "position_size": risk["position_size"],
        "risk_reward":   risk["risk_reward"],
        "reasons":       reasons,
    }
