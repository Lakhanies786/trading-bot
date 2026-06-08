import asyncio
import io
import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

app = FastAPI(title="MEXC Trading Bot", version="7.0.0")

# ── In-memory signal log (persisted to signal_log.json) ──────────────
SIGNAL_LOG_FILE = "signal_log.json"
MIN_CONFIDENCE  = 70      # FIX: raised from 50%
MIN_SCORE       = 10      # FIX: raised threshold
MIN_VOL_RATIO   = 0.5     # FIX: was 0.03x before
TRADE_HRS_UTC   = (8, 17) # FIX: time filter
REQUIRE_4H      = True    # FIX: 4H must agree
MAX_SIGNAL_AGE  = 24      # auto-expire after 24h

def load_signal_log() -> list:
    if not Path(SIGNAL_LOG_FILE).exists():
        return []
    try:
        with open(SIGNAL_LOG_FILE) as f:
            return json.load(f)
    except:
        return []

def save_signal_log(signals: list):
    with open(SIGNAL_LOG_FILE, "w") as f:
        json.dump(signals, f, indent=2)

signal_log: list = load_signal_log()


def get_spot():
    from mexc.client import MEXCSpotClient
    return MEXCSpotClient()

active_trades: dict = {}
_signal_state: dict = {}
CONFIRM_COUNT  = 3
COOLDOWN_SECS  = 900
_last_alert: dict = {}
ALERT_COOLDOWN = 900


def send_telegram(msg: str):
    try:
        import requests as req
        token   = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return
        req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg},
            timeout=5
        )
    except:
        pass


def maybe_send_alert(symbol, signal, price, sl, tp, rr, strength):
    now  = time.time()
    last = _last_alert.get(symbol, {})
    if last.get("signal") == signal and (now - last.get("sent_at", 0)) < ALERT_COOLDOWN:
        return
    _last_alert[symbol] = {"signal": signal, "sent_at": now}
    send_telegram(
        f"SIGNAL {signal} - {symbol}\n"
        f"Price: {price}\nSL: {sl}  TP: {tp}\nRR: {rr}\n{strength}"
    )


def stabilize_signal(symbol: str, raw_signal: str) -> str:
    now   = time.time()
    state = _signal_state.get(symbol, {
        "signal": "HOLD", "confirmed_at": 0, "count": 0, "candidate": "HOLD"
    })
    if state["signal"] != "HOLD" and (now - state["confirmed_at"]) < COOLDOWN_SECS:
        return state["signal"]
    if raw_signal == state.get("candidate"):
        state["count"] += 1
    else:
        state["candidate"] = raw_signal
        state["count"]     = 1
    if state["count"] >= CONFIRM_COUNT:
        state["signal"]       = raw_signal
        state["confirmed_at"] = now
        state["count"]        = 0
    _signal_state[symbol] = state
    return state["signal"]


def compute_signal(symbol: str) -> dict:
    from strategy.indicators import prepare_dataframe, add_indicators, generate_signal
    from strategy.orderbook  import analyze_orderbook

    spot = get_spot()

    klines_15m = spot.get_klines(symbol, interval="15m", limit=200)
    klines_1h  = spot.get_klines(symbol, interval="1h",  limit=200)
    klines_4h  = spot.get_klines(symbol, interval="4h",  limit=200)

    df_15m = add_indicators(prepare_dataframe(klines_15m))
    df_1h  = add_indicators(prepare_dataframe(klines_1h))
    df_4h  = add_indicators(prepare_dataframe(klines_4h))

    sig_15m = generate_signal(df_15m)
    sig_1h  = generate_signal(df_1h)
    sig_4h  = generate_signal(df_4h)

    orderbook   = spot.get_orderbook(symbol, limit=50)
    ob_analysis = analyze_orderbook(orderbook, sig_1h["price"])
    ob_signal   = ob_analysis["ob_signal"]

    signals    = [sig_15m["signal"], sig_1h["signal"], sig_4h["signal"]]
    buy_count  = signals.count("BUY")
    sell_count = signals.count("SELL")

    if buy_count >= 2:
        raw_signal     = "BUY"
        mtf_confidence = "HIGH" if buy_count == 3 else "MEDIUM"
        mtf_agreement  = f"✅ {buy_count}/3 timeframes say BUY"
    elif sell_count >= 2:
        raw_signal     = "SELL"
        mtf_confidence = "HIGH" if sell_count == 3 else "MEDIUM"
        mtf_agreement  = f"✅ {sell_count}/3 timeframes say SELL"
    else:
        raw_signal     = "HOLD"
        mtf_confidence = "LOW"
        mtf_agreement  = "⏳ Timeframes disagree — wait"

    if raw_signal == ob_signal and raw_signal != "HOLD":
        final_strength = f"🔥 STRONG {raw_signal} — Indicators + Order Book confirm!"
    elif raw_signal in ("BUY", "SELL"):
        final_strength = f"✅ {raw_signal} — Indicators agree"
    else:
        final_strength = "⏳ HOLD — No clear direction"

    final_signal = stabilize_signal(symbol, raw_signal)
    if final_signal != raw_signal:
        final_strength = f"⏳ HOLD — Waiting to confirm ({raw_signal} pending)"

    main    = sig_1h
    price   = main["price"]
    atr_val = main["atr"]

    if final_signal == "BUY":
        stop_loss   = round(price - (atr_val * 1.5), 4)
        take_profit = round(price + (atr_val * 3.0), 4)
    elif final_signal == "SELL":
        stop_loss   = round(price + (atr_val * 1.5), 4)
        take_profit = round(price - (atr_val * 3.0), 4)
    else:
        stop_loss   = None
        take_profit = None

    if stop_loss and take_profit:
        risk        = abs(price - stop_loss)
        reward      = abs(take_profit - price)
        risk_reward = f"1:{round(reward/risk, 1)}" if risk > 0 else "1:2"
    else:
        risk_reward = "N/A"

    all_reasons = [final_strength, mtf_agreement] + ob_analysis["ob_reasons"] + main["reasons"]

    if final_signal in ("BUY", "SELL") and stop_loss and take_profit:
        maybe_send_alert(symbol, final_signal, price, stop_loss, take_profit, risk_reward, final_strength)

    return {
        "symbol":             symbol,
        "signal":             final_signal,
        "confidence":         main.get("confidence", "0%"),
        "mtf_confidence":     mtf_confidence,
        "agreement":          final_strength,
        "timeframes": {
            "15m": sig_15m["signal"],
            "1h":  sig_1h["signal"],
            "4h":  sig_4h["signal"],
        },
        "ob_signal":          ob_signal,
        "bid_ask_ratio":      ob_analysis["bid_ask_ratio"],
        "ob_imbalance":       ob_analysis.get("imbalance", 0),
        "ob_liquidity":       ob_analysis.get("liquidity", 0),
        "ob_spread_pct":      ob_analysis.get("spread_pct", 0),
        "total_bid_volume":   ob_analysis["total_bid_volume"],
        "total_ask_volume":   ob_analysis["total_ask_volume"],
        "biggest_buy_wall":   ob_analysis["biggest_buy_wall"],
        "biggest_sell_wall":  ob_analysis["biggest_sell_wall"],
        "buy_wall_price":     ob_analysis["buy_wall_price"],
        "sell_wall_price":    ob_analysis["sell_wall_price"],
        "price":              price,
        "rsi":                main["rsi"],
        "macd_hist":          main["macd_hist"],
        "ema9":               main["ema9"],
        "ema21":              main["ema21"],
        "ema50":              main["ema50"],
        "adx":                main["adx"],
        "adx_pos":            main.get("adx_pos", 0),
        "adx_neg":            main.get("adx_neg", 0),
        "cvd":                main.get("cvd", 0),
        "roc":                main.get("roc", 0),
        "bb_lower":           main["bb_lower"],
        "bb_upper":           main["bb_upper"],
        "atr":                main["atr"],
        "stop_loss":          stop_loss,
        "take_profit":        take_profit,
        "position_size":      main["position_size"],
        "risk_reward":        risk_reward,
        "score":              main.get("score"),
        "vwap":               main.get("vwap"),
        "vol_ratio":          main.get("vol_ratio"),
        "stoch_k":            main.get("stoch_k"),
        "nearest_support":    main.get("nearest_support"),
        "nearest_resistance": main.get("nearest_resistance"),
        "fib_618":            main.get("fib_618"),
        "fib_500":            main.get("fib_500"),
        "fib_382":            main.get("fib_382"),
        "reasons":            all_reasons,
    }


# ── Signal log helpers ────────────────────────────────────────────────

def passes_filters(sig: dict) -> bool:
    """All fixes applied — signal must pass every filter to be logged."""
    if sig.get("signal") == "HOLD":
        return False
    conf = int(sig.get("confidence", "0%").replace("%", ""))
    if conf < MIN_CONFIDENCE:
        return False
    score_num = int(sig.get("score", "0/16").split("/")[0])
    if score_num < MIN_SCORE:
        return False
    if (sig.get("vol_ratio") or 0) < MIN_VOL_RATIO:
        return False
    hour_utc = datetime.now(timezone.utc).hour
    if not (TRADE_HRS_UTC[0] <= hour_utc < TRADE_HRS_UTC[1]):
        return False
    if REQUIRE_4H:
        tf4h = sig.get("timeframes", {}).get("4h", "HOLD")
        if sig["signal"] == "BUY"  and tf4h == "SELL": return False
        if sig["signal"] == "SELL" and tf4h == "BUY":  return False
    if not sig.get("stop_loss") or not sig.get("take_profit"):
        return False
    return True


def log_signal_entry(sig: dict):
    """Log a new signal to the in-memory + file log."""
    global signal_log
    symbol = sig.get("symbol", "")
    signal = sig.get("signal", "HOLD")
    now    = datetime.now(timezone.utc)

    # Deduplicate — skip same symbol+direction open in last 2h
    two_hrs_ago = (now - timedelta(hours=2)).isoformat()
    for s in signal_log:
        if (s["symbol"] == symbol and s["signal"] == signal
                and s["status"] == "OPEN" and s["logged_at"] > two_hrs_ago):
            return

    tf = sig.get("timeframes", {})
    signal_log.append({
        "id":           f"{symbol}_{now.strftime('%Y%m%d_%H%M')}",
        "logged_at":    now.isoformat(),
        "date":         now.strftime("%Y-%m-%d"),
        "time_utc":     now.strftime("%H:%M"),
        "symbol":       symbol,
        "signal":       signal,
        "confidence":   sig.get("confidence", "0%"),
        "score":        sig.get("score", "0/16"),
        "vol_ratio":    round(sig.get("vol_ratio") or 0, 2),
        "tf_15m":       tf.get("15m", "-"),
        "tf_1h":        tf.get("1h", "-"),
        "tf_4h":        tf.get("4h", "-"),
        "entry_price":  round(sig.get("price", 0), 6),
        "stop_loss":    round(sig.get("stop_loss", 0), 6),
        "take_profit":  round(sig.get("take_profit", 0), 6),
        "risk_reward":  sig.get("risk_reward", "N/A"),
        "rsi":          round(sig.get("rsi") or 0, 2),
        "macd":         round(sig.get("macd_hist") or 0, 6),
        "adx":          round(sig.get("adx") or 0, 2),
        "fib_618":      round(sig.get("fib_618") or 0, 6),
        "fib_500":      round(sig.get("fib_500") or 0, 6),
        "fib_382":      round(sig.get("fib_382") or 0, 6),
        "nearest_support":    round(sig.get("nearest_support") or 0, 6),
        "nearest_resistance": round(sig.get("nearest_resistance") or 0, 6),
        "status":       "OPEN",
        "outcome":      "-",
        "exit_price":   None,
        "exit_time":    None,
        "pnl_pct":      None,
        "hours_open":   0,
    })
    save_signal_log(signal_log)


def update_signal_outcomes():
    """Check open signals against current price — mark WIN/LOSS/EXPIRED."""
    global signal_log
    changed = False
    for sig in signal_log:
        if sig["status"] != "OPEN":
            continue
        try:
            import requests as req
            r     = req.get(f"https://api.mexc.com/api/v3/ticker/price?symbol={sig['symbol']}", timeout=5)
            price = float(r.json()["price"])
        except:
            continue

        direction = sig["signal"]
        sl        = sig["stop_loss"]
        tp        = sig["take_profit"]
        entry     = sig["entry_price"]
        logged_at = datetime.fromisoformat(sig["logged_at"])
        hours_open = round((datetime.now(timezone.utc) - logged_at).total_seconds() / 3600, 1)
        sig["hours_open"] = hours_open

        outcome    = None
        exit_price = None

        if direction == "BUY":
            if price <= sl:   outcome = "LOSS"; exit_price = sl
            elif price >= tp: outcome = "WIN";  exit_price = tp
        elif direction == "SELL":
            if price >= sl:   outcome = "LOSS"; exit_price = sl
            elif price <= tp: outcome = "WIN";  exit_price = tp

        if not outcome and hours_open >= MAX_SIGNAL_AGE:
            outcome = "EXPIRED"; exit_price = price

        if outcome:
            sig["status"]     = "CLOSED"
            sig["outcome"]    = outcome
            sig["exit_price"] = round(exit_price, 6)
            sig["exit_time"]  = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
            if direction == "BUY":
                sig["pnl_pct"] = round(((exit_price - entry) / entry) * 100, 4)
            else:
                sig["pnl_pct"] = round(((entry - exit_price) / entry) * 100, 4)
            changed = True

    if changed:
        save_signal_log(signal_log)


# ── Excel generator (returns bytes for download) ──────────────────────

def generate_excel_bytes() -> bytes:
    import xlsxwriter
    import pandas as pd

    update_signal_outcomes()
    signals = signal_log

    buf = io.BytesIO()
    df  = pd.DataFrame(signals) if signals else pd.DataFrame()

    wb = xlsxwriter.Workbook(buf, {"in_memory": True})

    title  = wb.add_format({"bold":True,"font_size":13,"font_color":"#58A6FF","bg_color":"#0D1117"})
    hdr    = wb.add_format({"bold":True,"bg_color":"#161B22","font_color":"#FFFFFF","border":1,"align":"center"})
    win_f  = wb.add_format({"bg_color":"#0D3321","font_color":"#3FB950","border":1})
    loss_f = wb.add_format({"bg_color":"#3D1212","font_color":"#F85149","border":1})
    open_f = wb.add_format({"bg_color":"#1C2333","font_color":"#F0B429","border":1})
    exp_f  = wb.add_format({"bg_color":"#21262D","font_color":"#8B949E","border":1})
    neu    = wb.add_format({"bg_color":"#161B22","font_color":"#FFFFFF","border":1})
    grn    = wb.add_format({"bold":True,"bg_color":"#161B22","font_color":"#3FB950","border":1})
    red    = wb.add_format({"bold":True,"bg_color":"#161B22","font_color":"#F85149","border":1})
    ylw    = wb.add_format({"bold":True,"bg_color":"#161B22","font_color":"#F0B429","border":1})
    subhdr = wb.add_format({"bold":True,"bg_color":"#21262D","font_color":"#58A6FF","border":1})

    def rfmt(status):
        return {"WIN":win_f,"LOSS":loss_f,"OPEN":open_f,"EXPIRED":exp_f}.get(status, neu)

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Sheet 1: All Signals ─────────────────────────────────────────
    ws1 = wb.add_worksheet("All Signals"); ws1.set_tab_color("#58A6FF"); ws1.freeze_panes(3,0)
    ws1.write(0,0,f"MEXC Bot — All Signals Tracker  ({now_str})",title)

    cols = [
        ("Date","date",10),("Time UTC","time_utc",8),("Symbol","symbol",10),
        ("Signal","signal",7),("Score","score",8),("Confidence","confidence",10),
        ("Vol Ratio","vol_ratio",9),("15m","tf_15m",6),("1h","tf_1h",6),("4h","tf_4h",6),
        ("Entry","entry_price",13),("Stop Loss","stop_loss",13),("Take Profit","take_profit",13),
        ("R:R","risk_reward",8),("RSI","rsi",7),("ADX","adx",7),
        ("Fib 61.8%","fib_618",12),("Fib 50%","fib_500",10),("Fib 38.2%","fib_382",12),
        ("Support","nearest_support",13),("Resistance","nearest_resistance",13),
        ("Status","status",9),("Outcome","outcome",9),
        ("Exit Price","exit_price",13),("Exit Time","exit_time",16),
        ("Hours Open","hours_open",10),("PnL %","pnl_pct",9),
    ]
    for c,(h,_,w) in enumerate(cols):
        ws1.set_column(c,c,w); ws1.write(2,c,h,hdr)

    if not df.empty:
        for r,row in enumerate(df.itertuples(),start=3):
            rf = rfmt(getattr(row,"status","-"))
            for c,(_,col,_) in enumerate(cols):
                val = getattr(row,col,"")
                if val is None: val="-"
                ws1.write(r,c,val,rf)
    else:
        ws1.write(3,0,"No signals logged yet — signals appear here automatically",ylw)

    # ── Sheet 2: Open Signals ─────────────────────────────────────────
    ws2 = wb.add_worksheet("🟡 Open Signals"); ws2.set_tab_color("#F0B429"); ws2.freeze_panes(3,0)
    open_df = df[df["status"]=="OPEN"] if not df.empty and "status" in df.columns else pd.DataFrame()
    ws2.write(0,0,f"Currently Open — {len(open_df)} signals being tracked  ({now_str})",title)
    ocols = [("Date","date",10),("Time","time_utc",8),("Symbol","symbol",10),
             ("Signal","signal",7),("Score","score",8),("Conf","confidence",10),
             ("4H","tf_4h",6),("Entry","entry_price",13),
             ("SL","stop_loss",13),("TP","take_profit",13),
             ("R:R","risk_reward",8),("Hrs Open","hours_open",9),
             ("RSI","rsi",7),("Fib 61.8%","fib_618",12),("Support","nearest_support",12)]
    for c,(h,_,w) in enumerate(ocols):
        ws2.set_column(c,c,w); ws2.write(2,c,h,hdr)
    if open_df.empty:
        ws2.write(3,0,"No open signals right now",ylw)
    else:
        for r,row in enumerate(open_df.itertuples(),start=3):
            for c,(_,col,_) in enumerate(ocols):
                val=getattr(row,col,"")
                if val is None: val="-"
                ws2.write(r,c,val,open_f)

    # ── Sheet 3: Daily Summary ────────────────────────────────────────
    ws3 = wb.add_worksheet("Daily Summary"); ws3.set_tab_color("#3FB950"); ws3.freeze_panes(3,0)
    ws3.write(0,0,"Daily Signal Performance",title)
    dh=["Date","Total","Wins","Losses","Expired","Open","Win Rate %","Total PnL %","Best %","Worst %"]
    for c,h in enumerate(dh): ws3.set_column(c,c,13); ws3.write(2,c,h,hdr)

    if not df.empty and "date" in df.columns:
        closed_df = df[df["status"]=="CLOSED"]
        daily = df.groupby("date").apply(lambda g: pd.Series({
            "total":   len(g),
            "wins":    int((g["outcome"]=="WIN").sum()),
            "losses":  int((g["outcome"]=="LOSS").sum()),
            "expired": int((g["outcome"]=="EXPIRED").sum()),
            "open_ct": int((g["status"]=="OPEN").sum()),
            "wr":      round((g["outcome"]=="WIN").sum()/max((g["outcome"].isin(["WIN","LOSS"])).sum(),1)*100,1),
            "pnl":     round(g["pnl_pct"].dropna().sum(),2),
            "best":    round(g["pnl_pct"].dropna().max(),2) if g["pnl_pct"].notna().any() else 0,
            "worst":   round(g["pnl_pct"].dropna().min(),2) if g["pnl_pct"].notna().any() else 0,
        })).reset_index()
        for r,row in enumerate(daily.itertuples(),start=3):
            rf = win_f if row.pnl>=0 else loss_f
            ws3.write(r,0,str(row.date)[:10],rf); ws3.write(r,1,int(row.total),rf)
            ws3.write(r,2,int(row.wins),win_f); ws3.write(r,3,int(row.losses),loss_f)
            ws3.write(r,4,int(row.expired),exp_f); ws3.write(r,5,int(row.open_ct),open_f)
            ws3.write(r,6,f"{row.wr}%",grn if row.wr>=55 else (ylw if row.wr>=45 else red))
            ws3.write(r,7,f"{row.pnl}%",rf)
            ws3.write(r,8,f"+{row.best}%",grn); ws3.write(r,9,f"{row.worst}%",red)
    else:
        ws3.write(3,0,"No data yet",ylw)

    # ── Sheet 4: By Symbol ────────────────────────────────────────────
    ws4 = wb.add_worksheet("By Symbol"); ws4.set_tab_color("#F85149"); ws4.freeze_panes(3,0)
    ws4.write(0,0,"Signal Performance by Symbol",title)
    sh=["Symbol","Total","Wins","Losses","Win Rate","Total PnL %","Avg Win %","Avg Loss %","Best","Worst"]
    for c,h in enumerate(sh): ws4.set_column(c,c,13); ws4.write(2,c,h,hdr)
    if not df.empty and "symbol" in df.columns:
        closed_df = df[df["status"]=="CLOSED"]
        if not closed_df.empty:
            for r,(sym,g) in enumerate(closed_df.groupby("symbol"),start=3):
                wns=g[g["outcome"]=="WIN"]; lss=g[g["outcome"]=="LOSS"]
                wr2=round(len(wns)/len(g)*100,1) if len(g)>0 else 0
                rf=win_f if wr2>=50 else loss_f
                ws4.write(r,0,sym,rf); ws4.write(r,1,len(g),rf)
                ws4.write(r,2,len(wns),win_f); ws4.write(r,3,len(lss),loss_f)
                ws4.write(r,4,f"{wr2}%",grn if wr2>=55 else (ylw if wr2>=45 else red))
                ws4.write(r,5,f"{round(g['pnl_pct'].dropna().sum(),2)}%",rf)
                ws4.write(r,6,f"+{round(wns['pnl_pct'].mean(),2)}%" if not wns.empty else "N/A",grn)
                ws4.write(r,7,f"{round(lss['pnl_pct'].mean(),2)}%" if not lss.empty else "N/A",red)
                ws4.write(r,8,f"+{round(g['pnl_pct'].dropna().max(),2)}%",grn)
                ws4.write(r,9,f"{round(g['pnl_pct'].dropna().min(),2)}%",red)
        else:
            ws4.write(3,0,"No closed signals yet",ylw)
    else:
        ws4.write(3,0,"No data yet",ylw)

    # ── Sheet 5: Dashboard ────────────────────────────────────────────
    ws5 = wb.add_worksheet("📊 Dashboard"); ws5.set_tab_color("#58A6FF")
    ws5.set_column("A:A",35); ws5.set_column("B:B",30)
    ws5.write(0,0,"📊 MEXC Bot — Live Signal Tracker Dashboard",title)
    ws5.write(1,0,f"Generated: {now_str}",neu)

    total_all  = len(signal_log)
    closed_all = [s for s in signal_log if s["status"]=="CLOSED"]
    wins_all   = sum(1 for s in closed_all if s["outcome"]=="WIN")
    losses_all = sum(1 for s in closed_all if s["outcome"]=="LOSS")
    exp_all    = sum(1 for s in closed_all if s["outcome"]=="EXPIRED")
    open_all   = sum(1 for s in signal_log if s["status"]=="OPEN")
    wr_all     = round(wins_all/max(wins_all+losses_all,1)*100,1)
    tpnl       = round(sum(s["pnl_pct"] for s in closed_all if s["pnl_pct"] is not None),2)
    aw         = round(sum(s["pnl_pct"] for s in closed_all if s["outcome"]=="WIN" and s["pnl_pct"])/max(wins_all,1),2) if wins_all>0 else 0
    al         = round(sum(s["pnl_pct"] for s in closed_all if s["outcome"]=="LOSS" and s["pnl_pct"] is not None)/max(losses_all,1),2) if losses_all>0 else 0

    stats = [
        ("── LIVE SIGNAL TRACKER ─────────────────",""),
        ("📋 Total Signals Logged",    total_all),
        ("✅ Wins — TP Hit",           wins_all),
        ("❌ Losses — SL Hit",         losses_all),
        ("⏰ Expired (24h timeout)",   exp_all),
        ("🟡 Currently Open",          open_all),
        ("🎯 Win Rate (closed only)",  f"{wr_all}%"),
        ("💰 Total PnL % (all closed)",f"{tpnl}%"),
        ("📈 Average Win %",           f"{aw}%"),
        ("📉 Average Loss %",          f"{al}%"),
        ("",""),
        ("── ALL FIXES ACTIVE ────────────────────",""),
        ("✅ Min Confidence",           f"{MIN_CONFIDENCE}%  ↑ raised from 50%"),
        ("✅ Min Score",                f"{MIN_SCORE}/16  ↑ raised threshold"),
        ("✅ Min Volume",               f"{MIN_VOL_RATIO}x  ↑ was 0.03x before"),
        ("✅ Time Filter",              f"{TRADE_HRS_UTC[0]}:00–{TRADE_HRS_UTC[1]}:00 UTC only"),
        ("✅ 4H Must Agree",            "ON — never trades against 4H trend"),
        ("✅ Fibonacci Entry Guide",    "Shows before entry — hides after"),
        ("✅ 4H Exit Warning",          "Only fires if 4H CHANGES after entry"),
        ("✅ Signal Age Limit",         f"{MAX_SIGNAL_AGE}h — auto-expire old signals"),
        ("",""),
        ("── SIGNAL HEALTH ───────────────────────",""),
    ]

    issues=[]
    if wr_all<50 and len(closed_all)>=10:
        issues.append(("❌ Win rate below 50%","Raise MIN_SCORE to 12 or MIN_CONFIDENCE to 75%"))
    if al and aw and abs(al)>aw:
        issues.append(("❌ Avg loss bigger than avg win","Tighten SL multiplier in indicators.py"))
    if tpnl<0 and len(closed_all)>=5:
        issues.append(("❌ Net negative PnL","Review signal quality — do NOT trade live yet"))
    if total_all<3:
        issues.append(("⚠️ Very few signals yet","Normal — signals accumulate over days"))
    if open_all>5:
        issues.append(("⚠️ Many signals still open","Price check may be delayed"))
    if not issues:
        if len(closed_all)>=5:
            issues.append(("✅ Bot signals look healthy!","Keep monitoring for consistency"))
        else:
            issues.append(("⏳ Still collecting data","Need 10+ closed signals to judge quality"))

    for r2,(lbl,val) in enumerate(stats+issues,start=3):
        if not lbl: continue
        sec=lbl.startswith("──")
        lf=subhdr if sec else (red if "❌" in lbl else (ylw if "⚠️" in lbl else (grn if "✅" in lbl else (ylw if "⏳" in lbl else neu))))
        isg=("Win Rate" in lbl and wr_all>=55) or ("PnL" in lbl and tpnl>=0)
        isb=("Win Rate" in lbl and wr_all<50 and len(closed_all)>=10) or ("PnL" in lbl and tpnl<0 and len(closed_all)>=5)
        vf=grn if isg else (red if isb else neu)
        ws5.write(r2,0,lbl,lf)
        if val!="": ws5.write(r2,1,str(val),vf)

    wb.close()
    buf.seek(0)
    return buf.read()


# ── Background tasks ─────────────────────────────────────────────────
PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT"]

async def background_scanner():
    await asyncio.sleep(10)
    while True:
        for symbol in PAIRS:
            try:
                sig = compute_signal(symbol)
                # Auto-log signal if it passes all filters
                if passes_filters(sig):
                    log_signal_entry(sig)
            except Exception as e:
                print(f"[Scanner] {symbol} error: {e}")
            await asyncio.sleep(3)
        # Update open signal outcomes every scan cycle
        try:
            update_signal_outcomes()
        except Exception as e:
            print(f"[Outcome updater] {e}")
        await asyncio.sleep(300)


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_scanner())


# ── Endpoints ─────────────────────────────────────────────────────────

@app.get("/")
def root():
    return {"status": "Bot running ✅", "version": "7.0.0"}

@app.get("/health")
def health():
    try:
        price = get_spot().get_ticker("BTCUSDT")
        return {"status": "ok ✅", "btc_price": price.get("price")}
    except Exception as e:
        return {"error": str(e)}

@app.get("/price/{symbol}")
def get_price(symbol: str):
    try:
        return get_spot().get_ticker(symbol)
    except Exception as e:
        return {"error": str(e)}

@app.get("/signal/{symbol}")
def get_signal(symbol: str = "BTCUSDT"):
    try:
        return compute_signal(symbol)
    except Exception as e:
        import traceback
        return {"error": repr(e), "trace": traceback.format_exc()}

@app.get("/scan/all")
def scan_all_pairs():
    results = []
    for symbol in PAIRS:
        try:
            sig = compute_signal(symbol)
            results.append({
                "symbol":     symbol,
                "signal":     sig["signal"],
                "confidence": sig["confidence"],
                "score":      sig["score"],
                "rsi":        sig["rsi"],
                "price":      sig["price"],
                "adx":        sig["adx"],
            })
        except Exception as e:
            results.append({"symbol": symbol, "error": str(e)})
    return {"pairs": results}

@app.get("/account/spot")
def get_spot_account():
    try:
        return get_spot().get_account()
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════
# NEW: Signal tracker endpoints
# ══════════════════════════════════════════════════════════════════════

@app.get("/signals/log")
def get_signal_log():
    """Returns all logged signals as JSON."""
    update_signal_outcomes()
    total   = len(signal_log)
    wins    = sum(1 for s in signal_log if s["outcome"] == "WIN")
    losses  = sum(1 for s in signal_log if s["outcome"] == "LOSS")
    open_ct = sum(1 for s in signal_log if s["status"] == "OPEN")
    return {
        "total":   total,
        "wins":    wins,
        "losses":  losses,
        "open":    open_ct,
        "win_rate": f"{round(wins/max(wins+losses,1)*100,1)}%",
        "signals": signal_log[-50:],  # last 50
    }

@app.get("/signals/stats")
def get_signal_stats():
    """Quick stats for Android app dashboard."""
    update_signal_outcomes()
    closed = [s for s in signal_log if s["status"] == "CLOSED"]
    wins   = sum(1 for s in closed if s["outcome"] == "WIN")
    losses = sum(1 for s in closed if s["outcome"] == "LOSS")
    tpnl   = round(sum(s["pnl_pct"] for s in closed if s["pnl_pct"] is not None), 2)
    return {
        "total_logged":  len(signal_log),
        "open":          sum(1 for s in signal_log if s["status"] == "OPEN"),
        "closed":        len(closed),
        "wins":          wins,
        "losses":        losses,
        "win_rate":      f"{round(wins/max(wins+losses,1)*100,1)}%",
        "total_pnl":     f"{tpnl}%",
        "last_updated":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }

@app.delete("/signals/clear")
def clear_signal_log():
    """Clear all logged signals (use carefully)."""
    global signal_log
    signal_log = []
    save_signal_log(signal_log)
    return {"status": "Signal log cleared"}


# ══════════════════════════════════════════════════════════════════════
# NEW: Excel download endpoint — call from Android app
# ══════════════════════════════════════════════════════════════════════

@app.get("/signals/download")
def download_signal_report():
    """
    Returns Excel file as download.
    Android app calls this URL — user taps Download Report button.
    Contains: All Signals, Open Signals, Daily Summary, By Symbol, Dashboard
    """
    try:
        excel_bytes = generate_excel_bytes()
        filename    = f"signal_report_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.xlsx"
        return StreamingResponse(
            io.BytesIO(excel_bytes),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="xlsxwriter not installed. Run: pip install xlsxwriter"
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Trade monitor endpoints ───────────────────────────────────────────

@app.post("/monitor/start")
async def start_monitor(
    symbol: str, direction: str, entry_price: float,
    stop_loss: float, take_profit: float, market: str = "spot"
):
    try:
        if symbol in active_trades:
            raise HTTPException(status_code=400, detail=f"{symbol} already monitored.")
        if entry_price <= 0 or stop_loss <= 0 or take_profit <= 0:
            raise HTTPException(status_code=400, detail="Invalid SL/TP/entry values.")
        if direction == "BUY" and stop_loss >= entry_price:
            raise HTTPException(status_code=400, detail="BUY: stop_loss must be below entry.")
        if direction == "SELL" and stop_loss <= entry_price:
            raise HTTPException(status_code=400, detail="SELL: stop_loss must be above entry.")
        if direction not in ("BUY", "SELL"):
            raise HTTPException(status_code=400, detail="direction must be BUY or SELL.")

        from monitor.trade_monitor import OpenTrade, monitor_trade
        trade = OpenTrade(
            symbol=symbol, direction=direction,
            entry_price=entry_price, stop_loss=stop_loss,
            take_profit=take_profit, market=market
        )
        active_trades[symbol] = trade
        asyncio.create_task(monitor_trade(trade))
        return {"status": "Monitoring started ✅", "symbol": symbol,
                "direction": direction, "entry_price": entry_price,
                "stop_loss": stop_loss, "take_profit": take_profit}
    except HTTPException:
        raise
    except Exception as e:
        return {"error": str(e)}

@app.get("/monitor/active")
def get_active_trades():
    if not active_trades:
        return {"message": "No active trades being monitored", "trades": {}}
    return {
        "trades": {
            symbol: {
                "direction":   t.direction,
                "entry_price": t.entry_price,
                "stop_loss":   t.stop_loss,
                "take_profit": t.take_profit,
                "trailing_sl": t.trailing_sl,
                "opened_at":   str(t.opened_at)
            }
            for symbol, t in active_trades.items()
        }
    }

@app.delete("/monitor/stop/{symbol}")
def stop_monitor(symbol: str):
    if symbol in active_trades:
        del active_trades[symbol]
        return {"status": f"Stopped monitoring {symbol} ✅"}
    return {"status": "Trade not found"}

from strategy.auto_trader import auto_trader

@app.get("/auto/scan")
def auto_scan():
    try:
        auto_trader.scan_and_trade()
        return {
            "status":      "Scan complete",
            "open_trades": len(auto_trader.open_trades),
            "trades":      auto_trader.open_trades,
            "history":     auto_trader.trade_history[-5:],
            "daily_pnl":   auto_trader.daily_pnl,
        }
    except Exception as e:
        return {"error": str(e)}

@app.get("/auto/status")
def auto_status():
    return {
        "open_trades":        auto_trader.open_trades,
        "total_closed":       len(auto_trader.trade_history),
        "history":            auto_trader.trade_history[-10:],
        "daily_pnl":          auto_trader.daily_pnl,
        "consecutive_losses": auto_trader.consecutive_losses,
    }

@app.get("/performance")
def performance():
    history = auto_trader.trade_history
    if not history:
        return {"message": "No trades yet"}
    wins      = [t for t in history if t["pnl_pct"] > 0]
    losses    = [t for t in history if t["pnl_pct"] <= 0]
    total_pnl = sum(t["pnl_pct"] for t in history)
    win_rate  = round(len(wins) / len(history) * 100, 1)
    avg_win   = round(sum(t["pnl_pct"] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss  = round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0
    return {
        "total_trades":  len(history),
        "wins":          len(wins),
        "losses":        len(losses),
        "win_rate":      f"{win_rate}%",
        "total_pnl":     f"{round(total_pnl,2)}%",
        "avg_win":       f"{avg_win}%",
        "avg_loss":      f"{avg_loss}%",
        "daily_pnl":     f"{auto_trader.daily_pnl:.2f}%",
        "recent_trades": history[-5:],
    }
