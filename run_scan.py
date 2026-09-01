"""Run the pre-market NIFTY 500 scan and publish dashboard state."""

from pathlib import Path
import json

from bot_engine import scan_nifty500


def records(frame):
    return frame.where(frame.notna(), None).to_dict(orient="records")


def main():
    result = scan_nifty500()
    state = {
        "generated_at": result["generated_at"],
        "market": result["market"],
        "breadth_universe": records(result["breadth_universe"]),
        "classified": records(result["classified"]),
        "buy_set": records(result["buy_set"]),
        "sell_set": records(result["sell_set"]),
        "errors": result["errors"],
    }
    path = Path("scan_state.json")
    path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    print(f"Scan complete: universe={len(state['breadth_universe'])}, classified={len(state['classified'])}, buy={len(state['buy_set'])}, sell={len(state['sell_set'])}, errors={len(state['errors'])}")


if __name__ == "__main__":
    main()
