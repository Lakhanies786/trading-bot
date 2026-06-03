def analyze_orderbook(orderbook: dict, current_price: float) -> dict:
    """
    Analyzes the MEXC order book to find:
    - Total buy volume vs sell volume
    - Bid/Ask ratio (>1 = more buyers, <1 = more sellers)
    - Biggest buy wall and sell wall near current price
    - Order book signal: BUY / SELL / NEUTRAL
    """
    try:
        bids = orderbook.get("bids", [])  # buyers  [[price, quantity], ...]
        asks = orderbook.get("asks", [])  # sellers [[price, quantity], ...]

        if not bids or not asks:
            return _neutral("No order book data")

        # ── Total volume on each side ──────────────────
        total_bid_volume = sum(float(b[1]) for b in bids)
        total_ask_volume = sum(float(a[1]) for a in asks)

        # ── Bid/Ask ratio ─────────────────────────────
        # > 1.5 = strong buyers   < 0.7 = strong sellers
        if total_ask_volume == 0:
            ratio = 999
        else:
            ratio = round(total_bid_volume / total_ask_volume, 2)

        # ── Find biggest walls near current price ──────
        # Only look within 2% of current price
        price_range = current_price * 0.02

        near_bids = [b for b in bids if abs(float(b[0]) - current_price) <= price_range]
        near_asks = [a for a in asks if abs(float(a[0]) - current_price) <= price_range]

        biggest_buy_wall  = max((float(b[1]) for b in near_bids), default=0)
        biggest_sell_wall = max((float(a[1]) for a in near_asks), default=0)

        # ── Find biggest buy/sell wall prices ──────────
        buy_wall_price  = None
        sell_wall_price = None

        if near_bids:
            max_bid = max(near_bids, key=lambda b: float(b[1]))
            buy_wall_price = float(max_bid[0])

        if near_asks:
            max_ask = max(near_asks, key=lambda a: float(a[1]))
            sell_wall_price = float(max_ask[0])

        # ── Order Book Signal Logic ────────────────────
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
            ob_reasons.append(f"✅ Huge buy wall near price — support strong")
        elif biggest_sell_wall > biggest_buy_wall * 1.5:
            ob_signal = "SELL"
            ob_reasons.append(f"✅ Huge sell wall near price — resistance strong")

        return {
            "ob_signal":          ob_signal,
            "bid_ask_ratio":      ratio,
            "total_bid_volume":   round(total_bid_volume, 2),
            "total_ask_volume":   round(total_ask_volume, 2),
            "biggest_buy_wall":   round(biggest_buy_wall, 2),
            "biggest_sell_wall":  round(biggest_sell_wall, 2),
            "buy_wall_price":     buy_wall_price,
            "sell_wall_price":    sell_wall_price,
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
        "ob_reasons":        [reason],
    }
