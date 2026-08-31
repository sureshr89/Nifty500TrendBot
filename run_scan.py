"""GitHub worker entry point.

The pre-market mode builds the two trend sets from Dhan data.
The market mode reads the saved sets and only refreshes NIFTY 500 LTP.
"""

import json
import os
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from bot_engine import DhanClient, fetch_nifty500_index, fetch_equity_ohlc, scan_nifty500

IST = ZoneInfo("Asia/Kolkata")
ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "scan_state.json"
RANKING_FILE = ROOT / "monthly_stock_ranking.csv"

def now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

def write_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

def premarket():
    result = scan_nifty500()
    result["classified"].to_csv(RANKING_FILE, index=False)
    state = {
        "health": {"worker_status": "ok", "last_scan_ist": result["generated_at"], "last_error": ""},
        "market": result["market"],
        "buy_set": result["buy_set"].to_dict(orient="records"),
        "sell_set": result["sell_set"].to_dict(orient="records"),
        "scan_errors": result["errors"],
        "strategy": {"name": "S1", "pullback_pct": 0.15, "entry_start": "09:30", "entry_end": "13:00", "square_off": "14:55"},
        "s1": {"trades": [], "signals": []},
    }
    write_state(state)
    return state

def market():
    if not STATE_FILE.exists():
        raise RuntimeError("scan_state.json missing. Run premarket mode first.")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    client = DhanClient()
    ltp, pdc = fetch_nifty500_index(client)
    day_pct = (ltp - pdc) / pdc * 100 if pdc else 0
    previous_market = state.get("market", {})
    ad_ratio = float(previous_market.get("ad_ratio", 1.0))
    mode = "BUY" if day_pct > 0 and ad_ratio > 1 else ("SELL" if day_pct < 0 and ad_ratio < 1 else "NEUTRAL")

    now_dt = datetime.now(IST)
    hhmm = now_dt.strftime("%H:%M")
    pullback = 0.0015
    s1 = state.setdefault("s1", {"trades": [], "signals": []})
    trades = s1.setdefault("trades", [])
    open_trade = next((t for t in trades if t.get("status") == "OPEN"), None)

    # Forced square-off at 14:55 IST.
    if open_trade and hhmm >= "14:55":
        quotes = fetch_equity_ohlc(client, [open_trade["SecurityId"]])
        q = quotes.get(int(open_trade["SecurityId"]))
        if q:
            exit_price = q["ltp"]
            open_trade.update({"status": "CLOSED", "exit_price": exit_price, "exit_reason": "AUTO_SQUARE_OFF", "exit_time": now()})

    # Only one S1 position at a time. Entries 09:30 <= time < 13:00.
    if not open_trade and "09:30" <= hhmm < "13:00" and mode in ("BUY", "SELL"):
        candidates = state.get("buy_set" if mode == "BUY" else "sell_set", [])
        ids = [x["SecurityId"] for x in candidates]
        quotes = fetch_equity_ohlc(client, ids)
        for stock in candidates:
            sid = int(stock["SecurityId"])
            q = quotes.get(sid)
            if not q:
                continue
            pdh = float(stock.get("PDH", 0))
            pdl = float(stock.get("PDL", 0))
            if pdh <= 0 or pdl <= 0:
                continue

            if mode == "BUY":
                pulled_back = q["open"] > pdh and q["low"] <= pdh * (1 - pullback)
                crossed = q["ltp"] >= pdh
                if pulled_back and crossed:
                    sl = (pdh + pdl) / 2
                    risk = q["ltp"] - sl
                    if risk <= 0: continue
                    trade = {"strategy":"S1","side":"BUY","status":"OPEN","Symbol":stock["Symbol"],"SecurityId":sid,"entry_price":q["ltp"],"PDH":pdh,"PDL":pdl,"SL":sl,"target":q["ltp"] + risk * 2,"entry_time":now()}
                    trades.append(trade); s1["signals"]=[trade]; break
            else:
                retraced = q["open"] < pdl and q["high"] >= pdl * (1 + pullback)
                crossed = q["ltp"] <= pdl
                if retraced and crossed:
                    sl = (pdh + pdl) / 2
                    risk = sl - q["ltp"]
                    if risk <= 0: continue
                    trade = {"strategy":"S1","side":"SELL","status":"OPEN","Symbol":stock["Symbol"],"SecurityId":sid,"entry_price":q["ltp"],"PDH":pdh,"PDL":pdl,"SL":sl,"target":q["ltp"] - risk * 2,"entry_time":now()}
                    trades.append(trade); s1["signals"]=[trade]; break

    # Monitor open S1 trade for SL/target.
    open_trade = next((t for t in trades if t.get("status") == "OPEN"), None)
    if open_trade:
        q = fetch_equity_ohlc(client, [open_trade["SecurityId"]]).get(int(open_trade["SecurityId"]))
        if q:
            px = q["ltp"]
            if open_trade["side"] == "BUY":
                reason = "STOP_LOSS" if px <= open_trade["SL"] else ("TARGET" if px >= open_trade["target"] else "")
            else:
                reason = "STOP_LOSS" if px >= open_trade["SL"] else ("TARGET" if px <= open_trade["target"] else "")
            if reason:
                open_trade.update({"status":"CLOSED","exit_price":px,"exit_reason":reason,"exit_time":now()})

    state["market"] = {"ltp": ltp, "pdc": pdc, "day_pct": day_pct, "advances": previous_market.get("advances", 0), "declines": previous_market.get("declines", 0), "ad_ratio": ad_ratio, "mode": mode}
    state.setdefault("health", {}).update({"worker_status": "ok", "last_scan_ist": now(), "last_error": ""})
    write_state(state)
    return state

if __name__ == "__main__":
    mode = os.getenv("WORKER_MODE", "premarket").strip().lower()
    scans = max(1, int(os.getenv("MARKET_SCAN_COUNT", "1")))
    delay = max(15, int(os.getenv("MARKET_SCAN_SECONDS", "15")))
    if mode == "premarket":
        state = premarket()
        print(json.dumps({"buy_count": len(state["buy_set"]), "sell_count": len(state["sell_set"]), "market": state["market"]}, indent=2))
    elif mode == "market":
        for i in range(scans):
            state = market()
            print(f"Market LTP scan {i+1}/{scans}: {state['market']}")
            if i < scans - 1:
                time.sleep(delay)
    else:
        raise RuntimeError("WORKER_MODE must be premarket or market")
