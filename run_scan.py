"""Command-line Dhan scan helper for scheduled execution."""

import json
from pathlib import Path

from bot_engine import scan_nifty500

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "scan_state.json"
RANKING_FILE = ROOT / "monthly_stock_ranking.csv"

result = scan_nifty500()
result["classified"].to_csv(RANKING_FILE, index=False)

summary = {
    "generated_at": result["generated_at"],
    "market": result["market"],
    "buy_count": int(len(result["buy_set"])),
    "sell_count": int(len(result["sell_set"])),
    "errors": result["errors"],
}
STATE_FILE.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
print(json.dumps(summary, indent=2, default=str))
