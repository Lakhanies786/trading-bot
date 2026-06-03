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
    df["ema9"]  = ta.ema(df["close"], length=9)
    df["ema21"] = ta.ema(df["close"], length=21)
    df["ema50"] = ta.ema(df["close"], length=50)

    # ── RSI ────────────────────────────────────────────────
    df["rsi"] = ta.rsi(df["close"], length=14)

    # ── MACD ───────────────────────────────────────────────
    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    df["macd"]        = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    df["macd_hist"]   = macd["MACDh_12_26_9"]

    # ── Bollinger Bands ────────────────────────────────────
    bb = ta.bbands(df["close"], length=20, std=2)
    bb_col_upper = [c for c in bb.columns if c.startswith("BBU")][0]
    bb_col_mid   = [c for c in bb.columns if c.startswith("BBM")][0]
    bb_col_lower = [c for c in bb.columns if c.startswith("BBL")][0]
    df["bb_upper"] = bb[bb_col_upper]
    df["bb_mid"]   = bb[bb_col_mid]
    df["bb_lower"] = bb[bb_col_lower]

    # ── ATR (Volatility / Stop Loss sizing) ───────────────
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], length=14)

    # ── Volume Analysis ────────────────────────────────────
    df["vol_sma"] = ta.sma(df["volume"], length=20)
    df["high_volume"] = df["volume"] > (df["vol_sma"] * 1.5)

    # ── RSI Divergence (simple version) ───────────────────
    df["rsi_prev"] = df["rsi"].shift(1)
    df["price_prev"] = df["close"].shift(1)
    df["bearish_div"] = (df["close"] > df["price_prev"]) & (df["rsi"] < df["rsi_prev"])
    df["bullish_div"] = (df["close"] < df["price_prev"]) & (df["rsi"] > df["rsi_prev"])

    # ── Candlestick Patterns ───────────────────────────────
    df["body"]       = abs(df["close"] - df["open"])
    df["upper_wick"] = df["high"] - df[["close", "open"]].max(axis=1)
    df["lower_wick"] = df[["close", "open"]].min(axis=1) - df["low"]

    # Hammer: small body, long lower wick = bullish
    df["hammer"] = (
        (df["lower_wick"] > df["body"] * 2) &
        (df["upper_wick"] < df["body"] * 0.5)
    )
    # Shooting Star: small body, long upper wick = bearish
    df["shooting_star"] = (
        (df["upper_wick"] > df["body"] * 2) &
        (df["lower_wick"] < df["body"] * 0.5)
    )
    # Doji: very small body = indecision
    df["doji"] = df["body"] < (df["high"] - df["low"]) * 0.1

    # ── Trend Strength (ADX) ───────────────────────────────
    adx = ta.adx(df["high"], df["low"], df["close"], length=14)
    df["adx"] = adx["ADX_14"]
    df["strong_trend"] = df["adx"] > 25   # above 25 = strong trend

    return df


def calculate_risk_management(price: float, signal: str, atr: float) -> dict:
    """
    Calculates stop loss and take profit automatically using ATR.
    ATR = how much price normally moves — bigger ATR = wider stop loss needed.
    Risk:Reward ratio = 1:2 (risk $1 to make $2)
    """
    atr_multiplier_sl = 1.5   # stop loss = 1.5x ATR away
    atr_multiplier_tp = 3.0   # take profit = 3x ATR away (1:2 ratio)

    if signal == "BUY":
        stop_loss   = round(price - (atr * atr_multiplier_sl), 4)
        take_profit = round(price + (atr * atr_multiplier_tp), 4)
    elif signal == "SELL":
        stop_loss   = round(price + (atr * atr_multiplier_sl), 4)
        take_profit = round(price - (atr * atr_multiplier_tp), 4)
    else:
        stop_loss   = None
        take_profit = None

    # Position sizing: risk only 2% of $1000 account = $20 max loss
    account_size   = 1000
    risk_percent   = 0.02
    risk_amount    = account_size * risk_percent   # $20
    if stop_loss and price:
        sl_distance    = abs(price - stop_loss)
        position_size  = round(risk_amount / sl_distance, 6) if sl_distance > 0 else 0
    else:
        position_size  = 0

    return {
        "stop_loss":     stop_loss,
        "take_profit":   take_profit,
        "position_size": position_size,
        "risk_amount":   risk_amount,
        "risk_reward":   "1:2"
    }


def generate_signal(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    prev = df.iloc[-2]

    signal  = "HOLD"
    reasons = []
    score   = 0   # confidence score — more conditions met = higher score

    # ── EMA Cross ─────────────────────────────────────────
    ema_cross_up   = prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"]
    ema_cross_down = prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"]
    above_ema50    = last["close"] > last["ema50"]

    # ── RSI ───────────────────────────────────────────────
    rsi_ok_buy  = 40 < last["rsi"] < 70
    rsi_ok_sell = 30 < last["rsi"] < 60
    rsi_oversold     = last["rsi"] < 35    # extra strong buy signal
    rsi_overbought   = last["rsi"] > 65    # extra strong sell signal

    # ── MACD ──────────────────────────────────────────────
    macd_bullish = last["macd"] > last["macd_signal"]
    macd_bearish = last["macd"] < last["macd_signal"]

    # ── Bollinger Bands ───────────────────────────────────
    price           = last["close"]
    near_lower_band = price <= last["bb_mid"]
    near_upper_band = price >= last["bb_mid"]
    at_bb_lower     = price <= last["bb_lower"] * 1.005   # touching lower band
    at_bb_upper     = price >= last["bb_upper"] * 0.995   # touching upper band

    # ── Volume ────────────────────────────────────────────
    high_vol = bool(last["high_volume"])

    # ── Trend Strength ────────────────────────────────────
    strong_trend = bool(last["strong_trend"])

    # ── Candlestick Patterns ──────────────────────────────
    hammer        = bool(last["hammer"])
    shooting_star = bool(last["shooting_star"])

    # ── RSI Divergence ────────────────────────────────────
    bullish_div = bool(last["bullish_div"])
    bearish_div = bool(last["bearish_div"])

    # ══════════════════════════════════════════════════════
    # BUY SIGNAL — count how many conditions are met
    # ══════════════════════════════════════════════════════
    if ema_cross_up:
        score += 2
        reasons.append("✅ EMA 9 crossed above EMA 21")
    if above_ema50:
        score += 1
        reasons.append("✅ Price above EMA 50 (uptrend)")
    if rsi_ok_buy:
        score += 1
        reasons.append(f"✅ RSI {last['rsi']:.1f} in healthy buy range")
    if rsi_oversold:
        score += 2
        reasons.append(f"✅ RSI {last['rsi']:.1f} oversold — strong buy zone")
    if macd_bullish:
        score += 1
        reasons.append("✅ MACD bullish")
    if near_lower_band or at_bb_lower:
        score += 1
        reasons.append("✅ Price near BB lower — bounce expected")
    if high_vol and ema_cross_up:
        score += 2
        reasons.append("✅ High volume confirms move")
    if strong_trend:
        score += 1
        reasons.append("✅ Strong trend (ADX > 25)")
    if hammer:
        score += 2
        reasons.append("✅ Hammer candle — bullish reversal")
    if bullish_div:
        score += 2
        reasons.append("✅ RSI bullish divergence")

    # Need at least 5 points to give BUY signal
    if score >= 5 and (ema_cross_up or rsi_oversold or hammer):
        signal = "BUY"
    else:
        # Reset score for SELL check
        score   = 0
        reasons = []

        # ══════════════════════════════════════════════════
        # SELL SIGNAL
        # ══════════════════════════════════════════════════
        if ema_cross_down:
            score += 2
            reasons.append("✅ EMA 9 crossed below EMA 21")
        if not above_ema50:
            score += 1
            reasons.append("✅ Price below EMA 50 (downtrend)")
        if rsi_ok_sell:
            score += 1
            reasons.append(f"✅ RSI {last['rsi']:.1f} in sell range")
        if rsi_overbought:
            score += 2
            reasons.append(f"✅ RSI {last['rsi']:.1f} overbought — strong sell zone")
        if macd_bearish:
            score += 1
            reasons.append("✅ MACD bearish")
        if near_upper_band or at_bb_upper:
            score += 1
            reasons.append("✅ Price near BB upper — reversal expected")
        if high_vol and ema_cross_down:
            score += 2
            reasons.append("✅ High volume confirms move")
        if strong_trend:
            score += 1
            reasons.append("✅ Strong trend (ADX > 25)")
        if shooting_star:
            score += 2
            reasons.append("✅ Shooting star candle — bearish reversal")
        if bearish_div:
            score += 2
            reasons.append("✅ RSI bearish divergence")

        if score >= 5 and (ema_cross_down or rsi_overbought or shooting_star):
            signal = "SELL"
        else:
            reasons = ["No strong signal conditions met yet"]

    # ── Confidence % ──────────────────────────────────────
    max_score  = 15
    confidence = min(round((score / max_score) * 100), 100)

    # ── Risk Management ───────────────────────────────────
    atr  = last["atr"]
    risk = calculate_risk_management(price, signal, atr)

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
