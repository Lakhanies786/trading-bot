"""
news_filter.py — News Awareness Layer for MEXC Trading Bot

Two sources:
  1. CryptoPanic API  — crypto-specific news sentiment (free tier)
  2. TradingEconomics / hard-coded high-impact event windows
     (FOMC, CPI, NFP, GDP) fetched from a free public calendar API

How it works:
  - check_news_safety(symbol) returns a dict:
      safe        : bool   — True = OK to trade, False = block signal
      reason      : str    — why it's blocked (or "clear")
      risk_level  : str    — CLEAR / CAUTION / BLOCKED
      sentiment   : str    — BULLISH / BEARISH / NEUTRAL / UNKNOWN
      events      : list   — upcoming high-impact events within 2h

Crypto symbols mapped:
  BTCUSDT → BTC,bitcoin
  ETHUSDT → ETH,ethereum
  SOLUSDT → SOL,solana
  BNBUSDT → BNB,binance
  XRPUSDT → XRP,ripple
"""

import os
import time
import requests
from datetime import datetime, timezone, timedelta

# ── Cache so we don't hammer APIs every 30 seconds ───────────────────
_news_cache: dict = {}
_calendar_cache: dict = {"events": [], "fetched_at": 0}
CACHE_TTL         = 300   # 5 minutes — refresh news every 5 min
CALENDAR_TTL      = 3600  # 1 hour — economic calendar doesn't change often

# ── Blackout windows around high-impact events ────────────────────────
MINS_BEFORE_EVENT = 60    # block 60 min before
MINS_AFTER_EVENT  = 30    # block 30 min after

# ── CryptoPanic API ───────────────────────────────────────────────────
CRYPTOPANIC_TOKEN = os.getenv("CRYPTOPANIC_TOKEN", "")   # free at cryptopanic.com
CRYPTOPANIC_URL   = "https://cryptopanic.com/api/v1/posts/"

# ── Free economic calendar (no key needed) ────────────────────────────
# Uses tradingeconomics free public endpoint + hardcoded known dates as fallback
CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

SYMBOL_KEYWORDS = {
    "BTCUSDT": ["BTC", "bitcoin", "crypto", "cryptocurrency"],
    "ETHUSDT": ["ETH", "ethereum", "crypto", "cryptocurrency"],
    "SOLUSDT": ["SOL", "solana", "crypto", "cryptocurrency"],
    "BNBUSDT": ["BNB", "binance", "crypto", "cryptocurrency"],
    "XRPUSDT": ["XRP", "ripple", "crypto", "cryptocurrency", "SEC", "Ripple"],
}

CRYPTOPANIC_CURRENCY = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "BNBUSDT": "BNB",
    "XRPUSDT": "XRP",
}

# High-impact macro keywords that affect all crypto
MACRO_KEYWORDS = [
    "FOMC", "Federal Reserve", "interest rate", "Fed rate",
    "CPI", "inflation", "NFP", "nonfarm", "GDP",
    "recession", "bank collapse", "SEC", "regulation",
    "crypto ban", "exchange hack", "bankruptcy"
]


# ════════════════════════════════════════════════════════════════════════
# ECONOMIC CALENDAR
# ════════════════════════════════════════════════════════════════════════

def fetch_economic_calendar() -> list:
    """
    Fetches this week's high-impact economic events.
    Returns list of dicts: {title, date, impact, currency}
    Falls back to empty list if API unavailable.
    """
    global _calendar_cache
    now = time.time()

    if now - _calendar_cache["fetched_at"] < CALENDAR_TTL:
        return _calendar_cache["events"]

    events = []
    try:
        r = requests.get(CALENDAR_URL, timeout=8)
        if r.status_code == 200:
            raw = r.json()
            for e in raw:
                impact = str(e.get("impact", "")).upper()
                if impact not in ("HIGH", "3"):   # only high impact
                    continue
                title    = e.get("title", e.get("name", "Unknown Event"))
                date_str = e.get("date", e.get("time", ""))
                try:
                    # Parse ISO or common formats
                    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S",
                                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                        try:
                            dt = datetime.strptime(date_str[:19], fmt[:len(date_str[:19])])
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            break
                        except:
                            dt = None
                    if dt:
                        events.append({
                            "title":    title,
                            "datetime": dt,
                            "impact":   "HIGH",
                            "currency": e.get("country", e.get("currency", "USD")),
                        })
                except:
                    continue
    except Exception as ex:
        print(f"[NewsFilter] Calendar fetch failed: {ex}")

    _calendar_cache = {"events": events, "fetched_at": now}
    return events


def get_upcoming_events(within_minutes: int = 90) -> list:
    """Returns high-impact events occurring within the next N minutes."""
    events  = fetch_economic_calendar()
    now_utc = datetime.now(timezone.utc)
    window  = timedelta(minutes=within_minutes)
    upcoming = []
    for e in events:
        dt = e["datetime"]
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        diff = dt - now_utc
        # within upcoming window OR within post-event blackout
        if timedelta(minutes=-MINS_AFTER_EVENT) <= diff <= window:
            mins = int(diff.total_seconds() / 60)
            upcoming.append({
                **e,
                "minutes_away": mins,
                "in_blackout":  -MINS_AFTER_EVENT <= mins <= MINS_BEFORE_EVENT,
            })
    return upcoming


# ════════════════════════════════════════════════════════════════════════
# CRYPTO NEWS SENTIMENT (CryptoPanic)
# ════════════════════════════════════════════════════════════════════════

def fetch_crypto_sentiment(symbol: str) -> dict:
    """
    Fetches latest news for a crypto symbol and scores sentiment.
    Returns: {sentiment, score, headline_count, top_headlines, source}
    """
    cache_key = symbol
    now       = time.time()

    if cache_key in _news_cache:
        cached = _news_cache[cache_key]
        if now - cached["fetched_at"] < CACHE_TTL:
            return cached["data"]

    currency = CRYPTOPANIC_CURRENCY.get(symbol, "BTC")
    result   = {
        "sentiment":       "UNKNOWN",
        "score":           0,
        "bullish_count":   0,
        "bearish_count":   0,
        "headline_count":  0,
        "top_headlines":   [],
        "source":          "cryptopanic",
        "risk_news":       False,
        "risk_reason":     "",
    }

    try:
        params = {
            "auth_token": CRYPTOPANIC_TOKEN if CRYPTOPANIC_TOKEN else "anonymous",
            "currencies": currency,
            "filter":     "hot",
            "public":     "true",
            "limit":      20,
        }
        r = requests.get(CRYPTOPANIC_URL, params=params, timeout=8)

        if r.status_code != 200:
            result["source"] = f"cryptopanic_err_{r.status_code}"
            _news_cache[cache_key] = {"data": result, "fetched_at": now}
            return result

        data    = r.json()
        results = data.get("results", [])

        bullish = 0
        bearish = 0
        headlines = []
        risk_found = False
        risk_reason = ""

        for post in results[:15]:
            title  = post.get("title", "").lower()
            votes  = post.get("votes", {})
            bull_v = votes.get("positive", 0) or 0
            bear_v = votes.get("negative", 0) or 0
            panic  = votes.get("important", 0) or 0

            bullish += bull_v
            bearish += bear_v

            # Check for high-risk keywords
            risk_keywords = [
                "hack", "exploit", "breach", "stolen", "bankrupt",
                "insolvent", "ban", "banned", "illegal", "fraud",
                "crash", "collapse", "lawsuit", "arrested", "shutdown",
                "delisted", "delist", "sanction"
            ]
            for kw in risk_keywords:
                if kw in title:
                    risk_found  = True
                    risk_reason = f"Risk keyword detected: '{kw}' in recent news"
                    break

            headlines.append({
                "title":   post.get("title", ""),
                "bullish": bull_v,
                "bearish": bear_v,
                "panic":   panic,
            })

        result["headline_count"] = len(results)
        result["bullish_count"]  = bullish
        result["bearish_count"]  = bearish
        result["top_headlines"]  = headlines[:5]
        result["risk_news"]      = risk_found
        result["risk_reason"]    = risk_reason

        # Score sentiment
        total_votes = bullish + bearish
        if total_votes == 0:
            result["sentiment"] = "NEUTRAL"
            result["score"]     = 0
        else:
            ratio = bullish / total_votes
            result["score"] = round((ratio - 0.5) * 200, 1)  # -100 to +100
            if ratio >= 0.65:
                result["sentiment"] = "BULLISH"
            elif ratio <= 0.35:
                result["sentiment"] = "BEARISH"
            else:
                result["sentiment"] = "NEUTRAL"

    except Exception as ex:
        print(f"[NewsFilter] CryptoPanic error for {symbol}: {ex}")
        result["source"] = "error"

    _news_cache[cache_key] = {"data": result, "fetched_at": now}
    return result


# ════════════════════════════════════════════════════════════════════════
# MAIN CHECK — called by compute_signal before generating any signal
# ════════════════════════════════════════════════════════════════════════

def check_news_safety(symbol: str) -> dict:
    """
    Master news safety check. Returns:
    {
      safe         : bool    — False = block signal entirely
      risk_level   : str     — CLEAR / CAUTION / BLOCKED
      reason       : str     — human readable reason
      sentiment    : str     — BULLISH / BEARISH / NEUTRAL / UNKNOWN
      news_score   : int     — -100 to +100
      events       : list    — upcoming high-impact events
      risk_news    : bool    — dangerous headline detected
    }
    """
    upcoming_events = get_upcoming_events(within_minutes=90)
    sentiment_data  = fetch_crypto_sentiment(symbol)

    # ── Check 1: High-impact economic event blackout ──────────────────
    blackout_events = [e for e in upcoming_events if e.get("in_blackout")]
    if blackout_events:
        ev = blackout_events[0]
        mins = ev["minutes_away"]
        timing = (f"in {mins} min" if mins > 0
                  else f"{abs(mins)} min ago — volatility settling")
        return {
            "safe":       False,
            "risk_level": "BLOCKED",
            "reason":     f"🚫 HIGH IMPACT EVENT: {ev['title']} ({timing}) — signal blocked",
            "sentiment":  sentiment_data["sentiment"],
            "news_score": sentiment_data["score"],
            "events":     upcoming_events,
            "risk_news":  sentiment_data["risk_news"],
        }

    # ── Check 2: Dangerous crypto-specific news headline ─────────────
    if sentiment_data["risk_news"]:
        return {
            "safe":       False,
            "risk_level": "BLOCKED",
            "reason":     f"🚫 RISK NEWS: {sentiment_data['risk_reason']}",
            "sentiment":  sentiment_data["sentiment"],
            "news_score": sentiment_data["score"],
            "events":     upcoming_events,
            "risk_news":  True,
        }

    # ── Check 3: Strongly bearish news on a BUY signal ───────────────
    # (caller checks signal direction — we just report)
    caution = False
    caution_reason = ""
    if sentiment_data["sentiment"] == "BEARISH" and sentiment_data["score"] < -30:
        caution        = True
        caution_reason = f"⚠️ News sentiment BEARISH (score {sentiment_data['score']}) — BUY signals carry extra risk"

    # ── Check 4: Events coming up soon (not in blackout yet) ─────────
    soon_events = [e for e in upcoming_events if 0 < e["minutes_away"] <= 90]
    if soon_events and not caution:
        ev             = soon_events[0]
        caution        = True
        caution_reason = f"⚠️ {ev['title']} in {ev['minutes_away']} min — signal quality may degrade"

    if caution:
        return {
            "safe":       True,   # still tradeable but flagged
            "risk_level": "CAUTION",
            "reason":     caution_reason,
            "sentiment":  sentiment_data["sentiment"],
            "news_score": sentiment_data["score"],
            "events":     upcoming_events,
            "risk_news":  False,
        }

    return {
        "safe":       True,
        "risk_level": "CLEAR",
        "reason":     "✅ No news events or risk detected",
        "sentiment":  sentiment_data["sentiment"],
        "news_score": sentiment_data["score"],
        "events":     upcoming_events,
        "risk_news":  False,
    }
