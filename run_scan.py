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

from bot_engine import DhanClient, fetch_nifty500_index, scan_nifty500

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
    if day_pct > 0 and ad_ratio > 1:
        mode = "BUY"
    elif day_pct < 0 and ad_ratio < 1:
        mode = "SELL"
    else:
        mode = "NEUTRAL"
    state["market"] = {
        "ltp": ltp,
        "pdc": pdc,
        "day_pct": day_pct,
        "advances": previous_market.get("advances", 0),
        "declines": previous_market.get("declines", 0),
        "ad_ratio": ad_ratio,
        "mode": mode,
    }
    state.setdefault("health", {})
    state["health"].update({"worker_status": "ok", "last_scan_ist": now(), "last_error": ""})
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
