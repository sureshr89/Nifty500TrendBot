"""GitHub pre-market scanner entry point.

GitHub prepares the Dhan-based NIFTY 500 BUY/SELL sets.
Live 15-second LTP monitoring and S1/S2/S3 evaluation run in Streamlit.
"""

import json
from pathlib import Path

from bot_engine import scan_nifty500

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "scan_state.json"
RANKING_FILE = ROOT / "monthly_stock_ranking.csv"

def write_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

def premarket():
    result = scan_nifty500()
    result["classified"].to_csv(RANKING_FILE, index=False)
    state = {
        "health": {
            "worker_status": "ok",
            "last_scan_ist": result["generated_at"],
            "last_error": ""
        },
        "market": result["market"],
        "classified": result["classified"].to_dict(orient="records"),
        "buy_set": result["buy_set"].to_dict(orient="records"),
        "sell_set": result["sell_set"].to_dict(orient="records"),
        "scan_errors": result["errors"],
        "strategies": {
            "S1": {"enabled": True, "entry_start": "09:30", "entry_end": "13:00", "square_off": "14:55", "pullback_pct": 0.15},
            "S2": {"enabled": True, "entry_start": "09:30", "entry_end": "13:00", "square_off": "14:55", "pullback_pct": 0.15},
            "S3": {"enabled": True, "entry_start": "09:30", "entry_end": "13:00", "square_off": "14:55", "target_r": 1.25}
        }
    }
    write_state(state)
    return state

if __name__ == "__main__":
    state = premarket()
    print(json.dumps({
        "buy_count": len(state["buy_set"]),
        "sell_count": len(state["sell_set"]),
        "market": state["market"]
    }, indent=2))
