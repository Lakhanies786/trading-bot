import pandas as pd
import numpy as np
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


# ══════════════════════════════════════════════════════════════════════
# MARKET REGIME DETECTOR
# Returns: TRENDING_BULL | TRENDING_BEAR | RANGING | VOLATILE
# ══════════════════════════════════════════════════════════════════════
def detect_market_regime(df: pd.DataFrame) -> dict:
    """
    Detects what kind of market we are in right now.
    TRENDING  → ADX > 25, trade WITH trend only
    RANGING   → ADX < 20, signals are unreliable — skip
    VOLATILE  → ATR spike > 2x average — too dangerous — skip
    """
    last = df.iloc[-1]

    adx     = float(last["adx"])     if pd.notna(last["adx"])     else 0
    adx_pos = float(last["adx_pos"]) if pd.notna(last["adx_pos"]) else 0
    adx_neg = float(last["adx_neg"]) if pd.notna(last["adx_neg"]) else 0
    atr     = float(last["atr"])     if pd.notna(last["atr"])     else 0

    # ATR spike: compare current ATR to its 20-period average
    atr_avg = float(df["atr"].rolling(20).mean().iloc[-1]) if len(df) >= 20 else atr
    atr_ratio = atr / atr_avg if atr_avg > 0 else 1.0

    # Regime classification
    if atr_ratio > 2.0:
        regime = "VOLATILE"
        regime_reason = f"⚡ ATR spike {atr_ratio:.1f}x average — news/manipulation risk"
        tradeable = False
    elif adx >= 25 and adx_pos > adx_neg:
        regime = "TRENDING_BULL"
        regime_reason = f"📈 Strong bullish trend — ADX {adx:.1f}, +DI {adx_pos:.1f} leads"
        tradeable = True
    elif adx >= 25 and adx_neg > adx_pos:
        regime = "TRENDING_BEAR"
        regime_reason = f"📉 Strong bearish trend — ADX {adx:.1f}, -DI {adx_neg:.1f} leads"
        tradeable = True
    elif adx >= 20:
        regime = "WEAK_TREND"
        regime_reason = f"〰️ Weak trend — ADX {adx:.1f}, signals may be unreliable"
        tradeable = True   # allow but with caution
    else:
        regime = "RANGING"
        regime_reason = f"↔️ Ranging market — ADX {adx:.1f} — signals unreliable, skip"
        tradeable = False

    return {
        "regime":        regime,
        "regime_reason": regime_reason,
        "tradeable":     tradeable,
        "adx":           round(adx, 2),
        "atr_ratio":     round(atr_ratio, 2),
    }


# ══════════════════════════════════════════════════════════════════════
# DAILY BIAS (HTF — Higher Timeframe Bias)
# Tells us what the BIG money is doing on the daily chart
# ══════════════════════════════════════════════════════════════════════
def detect_daily_bias(df_1d: pd.DataFrame) -> dict:
    """
    Analyses the daily candle to determine overall market bias.
    Only trade in the direction of the daily bias.
    BUY signals when bias=BEARISH are blocked.
    SELL signals when bias=BULLISH are blocked.
    """
    if len(df_1d) < 50:
        return {"bias": "NEUTRAL", "bias_reason": "Insufficient daily data", "bias_strength": "WEAK"}

    df_1d = df_1d.copy()
    df_1d["ema21_d"]  = ta.trend.ema_indicator(df_1d["close"], window=21)
    df_1d["ema50_d"]  = ta.trend.ema_indicator(df_1d["close"], window=50)
    df_1d["adx_d"]    = ta.trend.adx(df_1d["high"], df_1d["low"], df_1d["close"], window=14)
    df_1d["adx_pos_d"]= ta.trend.adx_pos(df_1d["high"], df_1d["low"], df_1d["close"], window=14)
    df_1d["adx_neg_d"]= ta.trend.adx_neg(df_1d["high"], df_1d["low"], df_1d["close"], window=14)

    last  = df_1d.iloc[-1]
    prev  = df_1d.iloc[-2]
    price = float(last["close"])

    ema21_d   = float(last["ema21_d"])  if pd.notna(last["ema21_d"])  else price
    ema50_d   = float(last["ema50_d"])  if pd.notna(last["ema50_d"])  else price
    adx_d     = float(last["adx_d"])    if pd.notna(last["adx_d"])    else 0
    adx_pos_d = float(last["adx_pos_d"])if pd.notna(last["adx_pos_d"])else 0
    adx_neg_d = float(last["adx_neg_d"])if pd.notna(last["adx_neg_d"])else 0

    # Daily candle structure
    daily_bullish_candle = last["close"] > last["open"]
    price_above_ema21    = price > ema21_d
    price_above_ema50    = price > ema50_d
    ema21_above_ema50    = ema21_d > ema50_d

    bull_points = sum([daily_bullish_candle, price_above_ema21,
                       price_above_ema50, ema21_above_ema50, adx_pos_d > adx_neg_d])
    bear_points = sum([not daily_bullish_candle, not price_above_ema21,
                       not price_above_ema50, not ema21_above_ema50, adx_neg_d > adx_pos_d])

    if bull_points >= 4:
        bias          = "BULLISH"
        bias_strength = "STRONG" if bull_points == 5 else "MODERATE"
        bias_reason   = f"📅 Daily bias BULLISH — price above EMA21/50, bullish candle structure"
    elif bear_points >= 4:
        bias          = "BEARISH"
        bias_strength = "STRONG" if bear_points == 5 else "MODERATE"
        bias_reason   = f"📅 Daily bias BEARISH — price below EMA21/50, bearish candle structure"
    else:
        bias          = "NEUTRAL"
        bias_strength = "WEAK"
        bias_reason   = f"📅 Daily bias NEUTRAL — mixed signals, both directions allowed"

    return {
        "bias":          bias,
        "bias_strength": bias_strength,
        "bias_reason":   bias_reason,
        "ema21_d":       round(ema21_d, 4),
        "ema50_d":       round(ema50_d, 4),
        "adx_d":         round(adx_d, 2),
    }


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    # ── EMA Stack ─────────────────────────────────────────
    df["ema9"]   = ta.trend.ema_indicator(df["close"], window=9)
    df["ema21"]  = ta.trend.ema_indicator(df["close"], window=21)
    df["ema50"]  = ta.trend.ema_indicator(df["close"], window=50)
    df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)

    # ── RSI ───────────────────────────────────────────────
    df["rsi"] = ta.momentum.rsi(df["close"], window=14)

    # ── Stochastic RSI ────────────────────────────────────
    stoch = ta.momentum.StochRSIIndicator(df["close"], window=14)
    df["stoch_rsi_k"] = stoch.stochrsi_k()
    df["stoch_rsi_d"] = stoch.stochrsi_d()

    # ── MACD ──────────────────────────────────────────────
    macd = ta.trend.MACD(df["close"], window_slow=26, window_fast=12, window_sign=9)
    df["macd"]        = macd.macd()
    df["macd_signal"] = macd.macd_signal()
    df["macd_hist"]   = macd.macd_diff()

    # ── Bollinger Bands ───────────────────────────────────
    bb = ta.volatility.BollingerBands(df["close"], window=20, window_dev=2)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_mid"]   = bb.bollinger_mavg()
    df["bb_lower"] = bb.bollinger_lband()
    df["bb_width"] = bb.bollinger_wband()
    df["bb_pct"]   = bb.bollinger_pband()   # 0=at lower, 1=at upper

    # ── ATR ───────────────────────────────────────────────
    df["atr"] = ta.volatility.average_true_range(
        df["high"], df["low"], df["close"], window=14
    )

    # ── ADX ───────────────────────────────────────────────
    df["adx"] = ta.trend.adx(df["high"], df["low"], df["close"], window=14)
    df["adx_pos"] = ta.trend.adx_pos(df["high"], df["low"], df["close"], window=14)
    df["adx_neg"] = ta.trend.adx_neg(df["high"], df["low"], df["close"], window=14)

    # ── VWAP ──────────────────────────────────────────────
    df["vwap"] = ta.volume.volume_weighted_average_price(
        df["high"], df["low"], df["close"], df["volume"]
    )

    # ── Volume ────────────────────────────────────────────
    df["vol_sma"]   = df["volume"].rolling(window=20).mean()
    df["vol_ratio"] = df["volume"] / df["vol_sma"]

    # ── CVD — Cumulative Volume Delta (NEW) ───────────────
    # Approximation: taker buy volume = volume * (close-low)/(high-low)
    hl_range = (df["high"] - df["low"]).replace(0, np.nan)
    df["buy_vol"]  = df["volume"] * (df["close"] - df["low"]) / hl_range
    df["sell_vol"] = df["volume"] * (df["high"] - df["close"]) / hl_range
    df["delta"]    = df["buy_vol"] - df["sell_vol"]
    df["cvd"]      = df["delta"].cumsum()
    df["cvd_sma"]  = df["cvd"].rolling(window=10).mean()

    # ── Support / Resistance (NEW) ────────────────────────
    # Rolling pivot: support = rolling min of lows, resistance = rolling max of highs
    df["support"]    = df["low"].rolling(window=20).min()
    df["resistance"] = df["high"].rolling(window=20).max()

    # ── Fibonacci Levels (NEW) ────────────────────────────
    # Based on last 50 candles swing high/low
    swing_high = df["high"].rolling(window=50).max()
    swing_low  = df["low"].rolling(window=50).min()
    fib_range  = swing_high - swing_low
    df["fib_382"] = swing_high - fib_range * 0.382
    df["fib_500"] = swing_high - fib_range * 0.500
    df["fib_618"] = swing_high - fib_range * 0.618

    # ── Momentum (ROC) (NEW) ──────────────────────────────
    df["roc"] = df["close"].pct_change(periods=10) * 100  # 10-candle rate of change

    return df


def detect_support_resistance_levels(df: pd.DataFrame, lookback: int = 50) -> dict:
    """
    Find key S/R levels from recent price action pivots.
    Returns nearest support below price and resistance above price.
    """
    recent = df.tail(lookback)
    price  = float(df["close"].iloc[-1])

    # Find local highs and lows (pivot points)
    highs = []
    lows  = []
    for i in range(2, len(recent) - 2):
        h = recent["high"].iloc[i]
        l = recent["low"].iloc[i]
        if h > recent["high"].iloc[i-1] and h > recent["high"].iloc[i+1]:
            highs.append(h)
        if l < recent["low"].iloc[i-1] and l < recent["low"].iloc[i+1]:
            lows.append(l)

    # Nearest support below current price
    supports    = [l for l in lows if l < price]
    resistances = [h for h in highs if h > price]

    nearest_support    = max(supports)    if supports    else float(df["support"].iloc[-1])
    nearest_resistance = min(resistances) if resistances else float(df["resistance"].iloc[-1])

    return {
        "nearest_support":    round(nearest_support, 4),
        "nearest_resistance": round(nearest_resistance, 4),
        "dist_to_support_pct":    round(((price - nearest_support) / price) * 100, 2),
        "dist_to_resistance_pct": round(((nearest_resistance - price) / price) * 100, 2),
    }


def generate_signal(df: pd.DataFrame) -> dict:
    last  = df.iloc[-1]
    prev  = df.iloc[-2]
    price = float(last["close"])

    signal  = "HOLD"
    reasons = []
    score   = 0

    # ── Trend Filter (EMA200) ──────────────────────────────
    above_ema200 = price > float(last["ema200"]) if pd.notna(last["ema200"]) else True
    below_ema200 = price < float(last["ema200"]) if pd.notna(last["ema200"]) else True

    # ── EMA Crossover ──────────────────────────────────────
    ema_cross_up   = prev["ema9"] <= prev["ema21"] and last["ema9"] > last["ema21"]
    ema_cross_down = prev["ema9"] >= prev["ema21"] and last["ema9"] < last["ema21"]
    ema_bull_stack = last["ema9"] > last["ema21"] > last["ema50"]
    ema_bear_stack = last["ema9"] < last["ema21"] < last["ema50"]

    # ── RSI ────────────────────────────────────────────────
    rsi = float(last["rsi"])
    rsi_buy       = 35 < rsi < 65
    rsi_sell      = rsi > 55
    rsi_oversold  = rsi < 35
    rsi_overbought = rsi > 65

    # ── Stochastic RSI ─────────────────────────────────────
    stoch_k = float(last["stoch_rsi_k"]) if pd.notna(last["stoch_rsi_k"]) else 0.5
    stoch_cross_up   = (prev["stoch_rsi_k"] < prev["stoch_rsi_d"] and
                        last["stoch_rsi_k"] > last["stoch_rsi_d"])
    stoch_cross_down = (prev["stoch_rsi_k"] > prev["stoch_rsi_d"] and
                        last["stoch_rsi_k"] < last["stoch_rsi_d"])

    # ── MACD ───────────────────────────────────────────────
    macd_bullish    = last["macd"] > last["macd_signal"] and last["macd_hist"] > 0
    macd_bearish    = last["macd"] < last["macd_signal"] and last["macd_hist"] < 0
    macd_cross_up   = prev["macd"] <= prev["macd_signal"] and last["macd"] > last["macd_signal"]
    macd_cross_down = prev["macd"] >= prev["macd_signal"] and last["macd"] < last["macd_signal"]

    # ── VWAP ───────────────────────────────────────────────
    vwap       = float(last["vwap"]) if pd.notna(last["vwap"]) else price
    above_vwap = price > vwap
    below_vwap = price < vwap

    # ── Volume ─────────────────────────────────────────────
    vol_ratio   = float(last["vol_ratio"]) if pd.notna(last["vol_ratio"]) else 1.0
    high_volume = vol_ratio > 1.2

    # ── ADX ────────────────────────────────────────────────
    adx          = float(last["adx"])     if pd.notna(last["adx"])     else 0
    adx_pos      = float(last["adx_pos"]) if pd.notna(last["adx_pos"]) else 0
    adx_neg      = float(last["adx_neg"]) if pd.notna(last["adx_neg"]) else 0
    strong_trend = adx > 25
    adx_bullish  = adx_pos > adx_neg   # +DI > -DI = bullish trend
    adx_bearish  = adx_neg > adx_pos   # -DI > +DI = bearish trend

    # ── Bollinger Bands ────────────────────────────────────
    bb_squeeze = float(last["bb_width"]) < 0.02 if pd.notna(last["bb_width"]) else False
    near_lower = price <= last["bb_mid"]
    near_upper = price >= last["bb_mid"]
    bb_pct     = float(last["bb_pct"]) if pd.notna(last["bb_pct"]) else 0.5

    # ── CVD (NEW) ──────────────────────────────────────────
    cvd     = float(last["cvd"])     if pd.notna(last["cvd"])     else 0
    cvd_sma = float(last["cvd_sma"]) if pd.notna(last["cvd_sma"]) else 0
    cvd_bullish = cvd > cvd_sma   # real buying pressure rising
    cvd_bearish = cvd < cvd_sma   # real selling pressure rising

    # ── Momentum ROC (NEW) ─────────────────────────────────
    roc = float(last["roc"]) if pd.notna(last["roc"]) else 0
    momentum_bull = roc > 1.0    # price moved up >1% in 10 candles
    momentum_bear = roc < -1.0   # price moved down >1% in 10 candles

    # ── Fibonacci proximity (NEW) ──────────────────────────
    fib_618 = float(last["fib_618"]) if pd.notna(last["fib_618"]) else 0
    fib_382 = float(last["fib_382"]) if pd.notna(last["fib_382"]) else 0
    near_fib_support    = abs(price - fib_618) / price < 0.005  # within 0.5% of 61.8%
    near_fib_resistance = abs(price - fib_382) / price < 0.005  # within 0.5% of 38.2%

    # ── Support/Resistance levels (NEW) ───────────────────
    sr = detect_support_resistance_levels(df)
    near_support    = sr["dist_to_support_pct"] < 1.0      # within 1% of support
    near_resistance = sr["dist_to_resistance_pct"] < 1.0   # within 1% of resistance

    # ══════════════════════════════════════════════════════
    # BUY SCORE  (max 16 points now, up from 12)
    # ══════════════════════════════════════════════════════
    buy_score   = 0
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
    if strong_trend and adx_bullish:          # upgraded: need direction too
        buy_score += 2
        buy_reasons.append(f"Strong bullish trend ADX {adx:.0f} +DI>{adx_pos:.0f}")
    elif strong_trend:
        buy_score += 1
        buy_reasons.append(f"Strong trend ADX {adx:.0f}")
    if above_ema200:
        buy_score += 1
        buy_reasons.append("Above EMA200 — bull market")
    if near_lower:
        buy_score += 1
        buy_reasons.append("Price at BB support")
    # NEW additions
    if cvd_bullish:
        buy_score += 1
        buy_reasons.append("CVD rising — real buy pressure")
    if momentum_bull:
        buy_score += 1
        buy_reasons.append(f"Bullish momentum ROC {roc:.1f}%")
    if near_support:
        buy_score += 1
        buy_reasons.append(f"Near key support ${sr['nearest_support']}")
    if near_fib_support:
        buy_score += 1
        buy_reasons.append("Price at Fibonacci 61.8% support")

    # ══════════════════════════════════════════════════════
    # SELL SCORE  (max 16 points)
    # ══════════════════════════════════════════════════════
    sell_score   = 0
    sell_reasons = []

    if ema_cross_down or ema_bear_stack:
        sell_score += 2
        sell_reasons.append("EMA bearish stack/cross")
    if macd_bearish or macd_cross_down:
        sell_score += 2
        sell_reasons.append("MACD bearish")
    if rsi_sell or rsi_overbought:
        sell_score += 1
        sell_reasons.append(f"RSI {rsi:.1f} elevated")
    if stoch_cross_down or stoch_k > 0.7:
        sell_score += 2
        sell_reasons.append("Stoch RSI bearish")
    if below_vwap:
        sell_score += 1
        sell_reasons.append("Price below VWAP")
    if high_volume:
        sell_score += 1
        sell_reasons.append(f"High volume {vol_ratio:.1f}x")
    if strong_trend and adx_bearish:
        sell_score += 2
        sell_reasons.append(f"Strong bearish trend ADX {adx:.0f} -DI>{adx_neg:.0f}")
    elif strong_trend:
        sell_score += 1
        sell_reasons.append(f"Strong trend ADX {adx:.0f}")
    if below_ema200:
        sell_score += 1
        sell_reasons.append("Below EMA200 — bear market")
    if near_upper:
        sell_score += 1
        sell_reasons.append("Price at BB resistance")
    # NEW additions
    if cvd_bearish:
        sell_score += 1
        sell_reasons.append("CVD falling — real sell pressure")
    if momentum_bear:
        sell_score += 1
        sell_reasons.append(f"Bearish momentum ROC {roc:.1f}%")
    if near_resistance:
        sell_score += 1
        sell_reasons.append(f"Near key resistance ${sr['nearest_resistance']}")
    if near_fib_resistance:
        sell_score += 1
        sell_reasons.append("Price at Fibonacci 38.2% resistance")

    # ── Signal Decision (threshold: 8/16) ─────────────────
    MAX_SCORE = 16
    THRESHOLD = 8

    if buy_score >= THRESHOLD and buy_score > sell_score:
        signal  = "BUY"
        reasons = buy_reasons
        score   = buy_score
    elif sell_score >= THRESHOLD and sell_score > buy_score:
        signal  = "SELL"
        reasons = sell_reasons
        score   = sell_score
    else:
        signal  = "HOLD"
        reasons = ["Score too low — waiting for clearer signal"]
        score   = max(buy_score, sell_score)

    confidence = f"{int((score / MAX_SCORE) * 100)}%"

    # ── Risk Management with Fibonacci TP levels (NEW) ────
    atr_val = float(last["atr"]) if pd.notna(last["atr"]) else price * 0.01

    if signal == "BUY":
        stop_loss   = round(price - (atr_val * 1.5), 4)
        take_profit = round(price + (atr_val * 3.0), 4)
        # Tighten SL to nearest support if it's closer
        if sr["nearest_support"] > stop_loss:
            stop_loss = round(sr["nearest_support"] * 0.998, 4)  # just below support
    elif signal == "SELL":
        stop_loss   = round(price + (atr_val * 1.5), 4)
        take_profit = round(price - (atr_val * 3.0), 4)
        # Tighten SL to nearest resistance if it's closer
        if sr["nearest_resistance"] < stop_loss:
            stop_loss = round(sr["nearest_resistance"] * 1.002, 4)  # just above resistance
    else:
        stop_loss   = None
        take_profit = None

    if stop_loss is not None and take_profit is not None:
        risk          = abs(price - stop_loss)
        reward        = abs(take_profit - price)
        risk_reward   = f"1:{round(reward/risk, 1)}" if risk > 0 else "1:2"
        position_size = round(1000 * 0.015 / risk, 4) if risk > 0 else 0
    else:
        risk          = 0
        reward        = 0
        risk_reward   = "N/A"
        position_size = 0

    return {
        "signal":            signal,
        "confidence":        confidence,
        "score":             f"{score}/{MAX_SCORE}",
        "price":             round(price, 4),
        "rsi":               round(rsi, 2),
        "stoch_k":           round(stoch_k * 100, 2),
        "macd_hist":         round(float(last["macd_hist"]), 6),
        "ema9":              round(float(last["ema9"]), 4),
        "ema21":             round(float(last["ema21"]), 4),
        "ema50":             round(float(last["ema50"]), 4),
        "ema200":            round(float(last["ema200"]), 4) if pd.notna(last["ema200"]) else 0,
        "vwap":              round(vwap, 4),
        "bb_lower":          round(float(last["bb_lower"]), 4),
        "bb_upper":          round(float(last["bb_upper"]), 4),
        "bb_pct":            round(bb_pct, 3),
        "atr":               round(atr_val, 4),
        "adx":               round(adx, 2),
        "adx_pos":           round(adx_pos, 2),
        "adx_neg":           round(adx_neg, 2),
        "cvd":               round(cvd, 2),
        "roc":               round(roc, 2),
        "vol_ratio":         round(vol_ratio, 2),
        "above_ema200":      above_ema200,
        "high_volume":       high_volume,
        "strong_trend":      strong_trend,
        "nearest_support":   sr["nearest_support"],
        "nearest_resistance": sr["nearest_resistance"],
        "fib_618":           round(fib_618, 4),
        "fib_382":           round(fib_382, 4),
        "stop_loss":         stop_loss,
        "take_profit":       take_profit,
        "position_size":     position_size,
        "risk_reward":       risk_reward,
        "reasons":           reasons,
    }


# ══════════════════════════════════════════════════════════════════════
# BREAKOUT DETECTOR — Phase 1 (Warning) + Phase 2 (Confirmed Signal)
#
# Phase 1 — COMPRESSION: BB squeeze + volume drying up + price near
#            key resistance/support → "Watch this level" warning
#
# Phase 2 — BREAKOUT CONFIRMED: candle CLOSES above/below the level
#            with volume spike → full breakout signal with measured move
# ══════════════════════════════════════════════════════════════════════

def detect_breakout(df_1h: pd.DataFrame, df_4h: pd.DataFrame) -> dict:
    """
    Detects two stages:

    PHASE 1 — COMPRESSION (Pre-breakout warning):
      - Bollinger Band width in bottom 20% of last 50 candles (squeeze)
      - Volume ratio < 0.6x (market is quiet — accumulation)
      - Price within 0.5% of a key resistance or support level
      - ADX < 30 (not yet trending — energy building)
      → Returns: phase=1, status=COMPRESSION, direction=UP/DOWN

    PHASE 2 — BREAKOUT CONFIRMED:
      - Phase 1 conditions were recently true (squeeze existed)
      - Latest CLOSED candle broke and CLOSED beyond the key level
      - Volume on breakout candle > 2x average (real conviction)
      - 4H candle agrees with direction
      → Returns: phase=2, status=CONFIRMED, measured_move_target
    """

    if len(df_1h) < 60 or len(df_4h) < 20:
        return _no_breakout("Insufficient data")

    last   = df_1h.iloc[-1]
    prev   = df_1h.iloc[-2]
    price  = float(last["close"])

    # ── BB Width squeeze detection ────────────────────────────────────
    bb_width_series = df_1h["bb_width"].dropna().tail(50)
    bb_width_now    = float(last["bb_width"]) if pd.notna(last["bb_width"]) else 999
    bb_width_pct20  = float(bb_width_series.quantile(0.20))  # bottom 20% threshold
    is_squeeze      = bb_width_now <= bb_width_pct20

    # ── Volume drying up (accumulation phase) ────────────────────────
    vol_ratio = float(last["vol_ratio"]) if pd.notna(last["vol_ratio"]) else 1.0
    is_quiet  = vol_ratio < 0.7

    # ── Key resistance and support levels ────────────────────────────
    # Use rolling 30-candle high/low as the key levels
    lookback        = df_1h.tail(30)
    key_resistance  = float(lookback["high"].max())
    key_support     = float(lookback["low"].min())
    range_height    = key_resistance - key_support   # for measured move

    dist_to_res_pct = ((key_resistance - price) / price) * 100
    dist_to_sup_pct = ((price - key_support)    / price) * 100

    near_resistance = 0 < dist_to_res_pct < 0.8   # within 0.8% of resistance
    near_support    = 0 < dist_to_sup_pct < 0.8   # within 0.8% of support

    # ── ADX — not yet strongly trending (energy coiling) ─────────────
    adx = float(last["adx"]) if pd.notna(last["adx"]) else 0
    energy_coiling = adx < 30

    # ── 4H direction ──────────────────────────────────────────────────
    last_4h      = df_4h.iloc[-1]
    tf4h_bullish = last_4h["close"] > last_4h["open"] and float(last_4h.get("ema9", last_4h["close"])) > float(last_4h.get("ema21", last_4h["close"]))
    tf4h_bearish = last_4h["close"] < last_4h["open"] and float(last_4h.get("ema9", last_4h["close"])) < float(last_4h.get("ema21", last_4h["close"]))

    # ── Breakout candle: CLOSED beyond level with volume ─────────────
    breakout_vol        = vol_ratio > 2.0          # volume spike on breakout
    closed_above_res    = prev["close"] < key_resistance <= last["close"]   # just closed above
    closed_below_sup    = prev["close"] > key_support  >= last["close"]     # just closed below

    # Was there a squeeze in the last 10 candles? (squeeze → breakout)
    recent_squeeze = any(
        float(df_1h["bb_width"].iloc[i]) <= bb_width_pct20
        for i in range(-10, -1)
        if pd.notna(df_1h["bb_width"].iloc[i])
    )

    # ── PHASE 2: CONFIRMED BREAKOUT ───────────────────────────────────
    if closed_above_res and breakout_vol and recent_squeeze and tf4h_bullish:
        measured_target = round(key_resistance + range_height, 4)
        move_pct        = round((range_height / price) * 100, 2)
        return {
            "phase":            2,
            "status":           "BREAKOUT_UP",
            "direction":        "UP",
            "breakout_level":   round(key_resistance, 4),
            "measured_target":  measured_target,
            "move_pct":         move_pct,
            "range_height":     round(range_height, 4),
            "vol_ratio":        round(vol_ratio, 2),
            "bb_width":         round(bb_width_now, 4),
            "adx":              round(adx, 2),
            "message":          (
                f"🚀 BREAKOUT UP CONFIRMED — closed above ${key_resistance:,.4f} "
                f"with {vol_ratio:.1f}x volume. "
                f"Measured move target: ${measured_target:,.4f} (+{move_pct}%)"
            ),
        }

    if closed_below_sup and breakout_vol and recent_squeeze and tf4h_bearish:
        measured_target = round(key_support - range_height, 4)
        move_pct        = round((range_height / price) * 100, 2)
        return {
            "phase":            2,
            "status":           "BREAKOUT_DOWN",
            "direction":        "DOWN",
            "breakout_level":   round(key_support, 4),
            "measured_target":  measured_target,
            "move_pct":         move_pct,
            "range_height":     round(range_height, 4),
            "vol_ratio":        round(vol_ratio, 2),
            "bb_width":         round(bb_width_now, 4),
            "adx":              round(adx, 2),
            "message":          (
                f"🔻 BREAKOUT DOWN CONFIRMED — closed below ${key_support:,.4f} "
                f"with {vol_ratio:.1f}x volume. "
                f"Measured move target: ${measured_target:,.4f} (-{move_pct}%)"
            ),
        }

    # ── PHASE 1: COMPRESSION WARNING ─────────────────────────────────
    if is_squeeze and energy_coiling:
        if near_resistance:
            strength = _compression_strength(is_squeeze, is_quiet, near_resistance, adx)
            return {
                "phase":           1,
                "status":          "COMPRESSION",
                "direction":       "UP",
                "watch_level":     round(key_resistance, 4),
                "measured_target": round(key_resistance + range_height, 4),
                "move_pct":        round((range_height / price) * 100, 2),
                "vol_ratio":       round(vol_ratio, 2),
                "bb_width":        round(bb_width_now, 4),
                "adx":             round(adx, 2),
                "strength":        strength,
                "message":         (
                    f"⚡ COMPRESSION — Squeeze building near resistance ${key_resistance:,.4f}. "
                    f"Watch for breakout. Measured target: ${key_resistance + range_height:,.4f} "
                    f"(+{round((range_height/price)*100,1)}%). "
                    f"BB width: {bb_width_now:.4f} | Vol: {vol_ratio:.1f}x | ADX: {adx:.1f}"
                ),
            }
        if near_support:
            strength = _compression_strength(is_squeeze, is_quiet, near_support, adx)
            return {
                "phase":           1,
                "status":          "COMPRESSION",
                "direction":       "DOWN",
                "watch_level":     round(key_support, 4),
                "measured_target": round(key_support - range_height, 4),
                "move_pct":        round((range_height / price) * 100, 2),
                "vol_ratio":       round(vol_ratio, 2),
                "bb_width":        round(bb_width_now, 4),
                "adx":             round(adx, 2),
                "strength":        strength,
                "message":         (
                    f"⚡ COMPRESSION — Squeeze building near support ${key_support:,.4f}. "
                    f"Watch for breakdown. Measured target: ${key_support - range_height:,.4f} "
                    f"(-{round((range_height/price)*100,1)}%). "
                    f"BB width: {bb_width_now:.4f} | Vol: {vol_ratio:.1f}x | ADX: {adx:.1f}"
                ),
            }
        # Squeeze exists but not near a key level yet
        return {
            "phase":     1,
            "status":    "COMPRESSION",
            "direction": "UNKNOWN",
            "watch_level":     round(key_resistance, 4),
            "measured_target": None,
            "move_pct":        round((range_height / price) * 100, 2),
            "vol_ratio":       round(vol_ratio, 2),
            "bb_width":        round(bb_width_now, 4),
            "adx":             round(adx, 2),
            "strength":        "BUILDING",
            "message":         (
                f"⏳ SQUEEZE BUILDING — BB compressing, waiting for price to approach "
                f"resistance ${key_resistance:,.4f} or support ${key_support:,.4f}. "
                f"Vol: {vol_ratio:.1f}x | ADX: {adx:.1f}"
            ),
        }

    return _no_breakout(
        f"No setup — BB width {bb_width_now:.4f} (squeeze threshold {bb_width_pct20:.4f}), "
        f"ADX {adx:.1f}, vol {vol_ratio:.1f}x"
    )


def _compression_strength(squeeze: bool, quiet: bool, near_level: bool, adx: float) -> str:
    points = sum([squeeze, quiet, near_level, adx < 20])
    if points >= 3: return "STRONG"
    if points == 2: return "MODERATE"
    return "BUILDING"


def _no_breakout(reason: str = "") -> dict:
    return {
        "phase":           0,
        "status":          "NONE",
        "direction":       "NONE",
        "watch_level":     None,
        "measured_target": None,
        "move_pct":        0,
        "vol_ratio":       0,
        "bb_width":        0,
        "adx":             0,
        "strength":        "NONE",
        "message":         f"No breakout setup detected. {reason}",
    }
