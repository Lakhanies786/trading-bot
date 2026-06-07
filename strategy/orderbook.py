def analyze_orderbook(orderbook: dict, current_price: float) -> dict:
    """
    Analyzes the MEXC order book to find:
    - Total buy/sell volume & Bid/Ask ratio
    - Biggest buy wall and sell wall near price
    - Order book imbalance (NEW)
    - Liquidity depth score (NEW)
    - Order book signal: BUY / SELL / NEUTRAL
    """
    try:
        bids = orderbook.get("bids", [])
        asks = orderbook.get("asks", [])

        if not bids or not asks:
            return _neutral("No order book data")

        # ── Total volume ────────────────────────────────────────────
        total_bid_volume = sum(float(b[1]) for b in bids)
        total_ask_volume = sum(float(a[1]) for a in asks)

        if total_ask_volume == 0:
            ratio = 999
        else:
            ratio = round(total_bid_volume / total_ask_volume, 2)

        # ── Walls near price (2% range) ─────────────────────────────
        price_range = current_price * 0.02
        near_bids = [b for b in bids if abs(float(b[0]) - current_price) <= price_range]
        near_asks = [a for a in asks if abs(float(a[0]) - current_price) <= price_range]

        biggest_buy_wall  = max((float(b[1]) for b in near_bids), default=0)
        biggest_sell_wall = max((float(a[1]) for a in near_asks), default=0)

        buy_wall_price  = None
        sell_wall_price = None
        if near_bids:
            max_bid = max(near_bids, key=lambda b: float(b[1]))
            buy_wall_price = float(max_bid[0])
        if near_asks:
            max_ask = max(near_asks, key=lambda a: float(a[1]))
            sell_wall_price = float(max_ask[0])

        # ── NEW: Order Book Imbalance (top 10 levels) ───────────────
        top_bids = bids[:10]
        top_asks = asks[:10]
        top_bid_vol = sum(float(b[1]) for b in top_bids)
        top_ask_vol = sum(float(a[1]) for a in top_asks)
        total_top   = top_bid_vol + top_ask_vol
        imbalance   = round((top_bid_vol - top_ask_vol) / total_top, 3) if total_top > 0 else 0
        # +1.0 = all buyers, -1.0 = all sellers, 0 = balanced

        # ── NEW: Liquidity Depth Score ───────────────────────────────
        # How deep is the book within 1% of price?
        tight_range   = current_price * 0.01
        tight_bids    = [b for b in bids if abs(float(b[0]) - current_price) <= tight_range]
        tight_asks    = [a for a in asks if abs(float(a[0]) - current_price) <= tight_range]
        tight_bid_vol = sum(float(b[1]) for b in tight_bids)
        tight_ask_vol = sum(float(a[1]) for a in tight_asks)
        liquidity     = round(tight_bid_vol + tight_ask_vol, 2)

        # ── NEW: Spread % ────────────────────────────────────────────
        best_bid = float(bids[0][0]) if bids else current_price
        best_ask = float(asks[0][0]) if asks else current_price
        spread_pct = round(((best_ask - best_bid) / current_price) * 100, 4)
        tight_spread = spread_pct < 0.05   # <0.05% spread = liquid market

        # ── Signal Logic ─────────────────────────────────────────────
        ob_signal  = "NEUTRAL"
        ob_reasons = []

        if ratio >= 1.5:
            ob_signal = "BUY"
            ob_reasons.append(f"✅ Strong buyers — Bid/Ask ratio: {ratio}")
        elif ratio <= 0.7:
            ob_signal = "SELL"
            ob_reasons.append(f"✅ Strong sellers — Bid/Ask ratio: {ratio}")
        else:
            ob_reasons.append(f"⏳ Balanced market — Bid/Ask ratio: {ratio}")

        if biggest_buy_wall > biggest_sell_wall * 1.5:
            ob_signal = "BUY"
            ob_reasons.append("✅ Huge buy wall near price — support strong")
        elif biggest_sell_wall > biggest_buy_wall * 1.5:
            ob_signal = "SELL"
            ob_reasons.append("✅ Huge sell wall near price — resistance strong")

        # NEW: Imbalance signal
        if imbalance > 0.3:
            ob_signal = "BUY"
            ob_reasons.append(f"✅ Order book heavily skewed to buyers ({imbalance:.2f})")
        elif imbalance < -0.3:
            ob_signal = "SELL"
            ob_reasons.append(f"✅ Order book heavily skewed to sellers ({imbalance:.2f})")

        # NEW: Warn on wide spread (low liquidity = risky entry)
        if not tight_spread:
            ob_reasons.append(f"⚠️ Wide spread {spread_pct:.3f}% — low liquidity, be cautious")

        return {
            "ob_signal":          ob_signal,
            "bid_ask_ratio":      ratio,
            "total_bid_volume":   round(total_bid_volume, 2),
            "total_ask_volume":   round(total_ask_volume, 2),
            "biggest_buy_wall":   round(biggest_buy_wall, 2),
            "biggest_sell_wall":  round(biggest_sell_wall, 2),
            "buy_wall_price":     buy_wall_price,
            "sell_wall_price":    sell_wall_price,
            "imbalance":          imbalance,      # NEW
            "liquidity":          liquidity,      # NEW
            "spread_pct":         spread_pct,     # NEW
            "tight_spread":       tight_spread,   # NEW
            "ob_reasons":         ob_reasons,
        }

    except Exception as e:
        return _neutral(f"Order book error: {str(e)}")


def _neutral(reason: str) -> dict:
    return {
        "ob_signal":         "NEUTRAL",
        "bid_ask_ratio":     1.0,
        "total_bid_volume":  0,
        "total_ask_volume":  0,
        "biggest_buy_wall":  0,
        "biggest_sell_wall": 0,
        "buy_wall_price":    None,
        "sell_wall_price":   None,
        "imbalance":         0,
        "liquidity":         0,
        "spread_pct":        0,
        "tight_spread":      True,
        "ob_reasons":        [reason],
    }
