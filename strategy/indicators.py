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
    # EMA
    df["ema9"]  = ta.ema(df["close"], length=9)
    df["ema21"] = ta.ema(df["close"], length=21)
    df["ema50"] = ta.ema(df["close"], length=50)

    # RSI
    df["rsi"] = ta.rsi(df["close"], length=14)

    # MACD
    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    df["macd"]        = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    df["macd_hist"]   = macd["MACDh_12_26_9"]

    # Bollinger Bands
    bb = ta.bbands(df["close"], length=20, std=2)
    bb_upper_col = [c for c in bb.columns if c.startswith("BBU")][0]
    bb_mid_col   = [c for c in bb.columns if c.startswith("BBM")][0]
    bb_lower_col = [c for c in bb.columns if c.startswith("BBL")][0]
    df["bb_upper"] = bb[bb_upper_col]
    df["bb_mid"]   = bb[bb_mid_col]
    df["bb_lower"] = bb[bb_lower_col]

    # ATR
    atr = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["atr"] = atr

    # ADX
    adx = ta.adx(df["high"], df["low"], df["close"], length=14)
    adx_col = [c for c in adx.columns if c.startswith("ADX")][0]
    df["adx"] = adx[adx_col]

    return df

def generate_signal(df: pd.DataFrame) -> dict:
    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    price = float(last["close"])

    signal  = "HOLD"
    reasons = []

    # EMA crossover
    ema_cross_up   = prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"]
    ema_cross_down = prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"]

    # RSI
    rsi_ok_buy  = 40 < last["rsi"] < 70
    rsi_ok_sell = 30 < last["rsi"] < 60

    # MACD
    macd_bullish = last["macd"] > last["macd_signal"]
    macd_bearish = last["macd"] < last["macd_signal"]

    # Bollinger Bands
    near_lower = price <= last["bb_mid"]
    near_upper = price >= last["bb_mid"]

    # Count conditions
    buy_conditions  = sum([ema_cross_up, rsi_ok_buy, macd_bullish, near_lower])
    sell_conditions = sum([ema_cross_down, rsi_ok_sell, macd_bearish, near_upper])

    if buy_conditions >= 3:
        signal = "BUY"
        if ema_cross_up:   reasons.append("EMA 9 crossed above EMA 21")
        if rsi_ok_buy:     reasons.append(f"RSI {last['rsi']:.1f} in healthy range")
        if macd_bullish:   reasons.append("MACD bullish")
        if near_lower:     reasons.append("Price at/below BB midline")
    elif sell_conditions >= 3:
        signal = "SELL"
        if ema_cross_down: reasons.append("EMA 9 crossed below EMA 21")
        if rsi_ok_sell:    reasons.append(f"RSI {last['rsi']:.1f}")
        if macd_bearish:   reasons.append("MACD bearish")
        if near_upper:     reasons.append("Price at/above BB midline")

    # Confidence score
    met = buy_conditions if signal == "BUY" else sell_conditions
    confidence = f"{int((met/4)*100)}%"

    # Risk management
    atr_val  = float(last["atr"]) if pd.notna(last["atr"]) else price * 0.01
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

    return {
        "signal":        signal,
        "confidence":    confidence,
        "price":         round(price, 4),
        "rsi":           round(float(last["rsi"]), 2),
        "macd_hist":     round(float(last["macd_hist"]), 6),
        "ema9":          round(float(last["ema9"]), 4),
        "ema21":         round(float(last["ema21"]), 4),
        "ema50":         round(float(last["ema50"]), 4),
        "bb_lower":      round(float(last["bb_lower"]), 4),
        "bb_upper":      round(float(last["bb_upper"]), 4),
        "atr":           round(float(atr_val), 4),
        "adx":           round(float(last["adx"]), 2) if pd.notna(last["adx"]) else 0,
        "stop_loss":     stop_loss,
        "take_profit":   take_profit,
        "position_size": round(1000 * 0.015 / risk, 4) if risk > 0 else 0,
        "risk_reward":   risk_reward,
        "reasons":       reasons
    }