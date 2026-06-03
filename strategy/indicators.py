import pandas as pd
import ta

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
    df["ema9"]  = ta.trend.ema_indicator(df["close"], window=9)
    df["ema21"] = ta.trend.ema_indicator(df["close"], window=21)
    df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)

    # RSI
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

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

    # ATR
    df["atr"] = ta.volatility.average_true_range(
        df["high"], df["low"], df["close"], window=14
    )

    # ADX
    df["adx"] = ta.trend.adx(
        df["high"], df["low"], df["close"], window=14
    )

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
        if ema_cross_up: reasons.append("EMA 9 crossed above EMA 21")
        if rsi_ok_buy:   reasons.append(f"RSI {last['rsi']:.1f} healthy")
        if macd_bullish: reasons.append("MACD bullish")
        if near_lower:   reasons.append("Price at BB midline")
    elif sell_conditions >= 3:
        signal = "SELL"
        if ema_cross_down: reasons.append("EMA 9 crossed below EMA 21")
        if rsi_ok_sell:    reasons.append(f"RSI {last['rsi']:.1f}")
        if macd_bearish:   reasons.append("MACD bearish")
        if near_upper:     reasons.append("Price above BB midline")

    met        = buy_conditions if signal == "BUY" else sell_conditions
    confidence = f"{int((met/4)*100)}%"

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

    return {
        "signal":      signal,
        "confidence":  confidence,
        "price":       round(price, 4),
        "rsi":         round(float(last["rsi"]), 2),
        "macd_hist":   round(float(last["macd_hist"]), 6),
        "ema9":        round(float(last["ema9"]), 4),
        "ema21":       round(float(last["ema21"]), 4),
        "ema50":       round(float(last["ema50"]), 4),
        "bb_lower":    round(float(last["bb_lower"]), 4),
        "bb_upper":    round(float(last["bb_upper"]), 4),
        "atr":         round(float(atr_val), 4),
        "adx":         round(float(last["adx"]), 2) if pd.notna(last["adx"]) else 0,
        "stop_loss":   stop_loss,
        "take_profit": take_profit,
        "position_size": round(1000 * 0.015 / risk, 4) if risk > 0 else 0,
        "risk_reward": risk_reward,
        "reasons":     reasons
    }