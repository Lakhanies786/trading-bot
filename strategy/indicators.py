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
    df["ema9"]  = ta.ema(df["close"], length=9)
    df["ema21"] = ta.ema(df["close"], length=21)
    df["rsi"]   = ta.rsi(df["close"], length=14)
    macd = ta.macd(df["close"], fast=12, slow=26, signal=9)
    df["macd"]        = macd["MACD_12_26_9"]
    df["macd_signal"] = macd["MACDs_12_26_9"]
    df["macd_hist"]   = macd["MACDh_12_26_9"]
    bb = ta.bbands(df["close"], length=20, std=2)
    bb_col_upper = [c for c in bb.columns if c.startswith("BBU")][0]
    bb_col_mid   = [c for c in bb.columns if c.startswith("BBM")][0]
    bb_col_lower = [c for c in bb.columns if c.startswith("BBL")][0]
    df["bb_upper"] = bb[bb_col_upper]
    df["bb_mid"]   = bb[bb_col_mid]
    df["bb_lower"] = bb[bb_col_lower]
    return df

def generate_signal(df: pd.DataFrame) -> dict:
    last = df.iloc[-1]
    prev = df.iloc[-2]
    signal  = "HOLD"
    reasons = []
    ema_cross_up   = prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"]
    ema_cross_down = prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"]
    rsi_ok_buy  = 40 < last["rsi"] < 70
    rsi_ok_sell = 30 < last["rsi"] < 60
    macd_bullish = last["macd"] > last["macd_signal"]
    macd_bearish = last["macd"] < last["macd_signal"]
    price           = last["close"]
    near_lower_band = price <= last["bb_mid"]
    near_upper_band = price >= last["bb_mid"]
    if ema_cross_up and rsi_ok_buy and macd_bullish and near_lower_band:
        signal = "BUY"
        reasons.append("EMA 9 crossed above EMA 21")
        reasons.append(f"RSI at {last['rsi']:.1f} healthy range")
        reasons.append("MACD bullish confirmation")
        reasons.append("Price at BB midline")
    elif ema_cross_down and rsi_ok_sell and macd_bearish and near_upper_band:
        signal = "SELL"
        reasons.append("EMA 9 crossed below EMA 21")
        reasons.append(f"RSI at {last['rsi']:.1f}")
        reasons.append("MACD bearish confirmation")
        reasons.append("Price above BB midline")
    return {
        "signal":    signal,
        "price":     round(price, 4),
        "rsi":       round(last["rsi"], 2),
        "macd_hist": round(last["macd_hist"], 6),
        "ema9":      round(last["ema9"], 4),
        "ema21":     round(last["ema21"], 4),
        "bb_lower":  round(last["bb_lower"], 4),
        "bb_upper":  round(last["bb_upper"], 4),
        "reasons":   reasons
    }
