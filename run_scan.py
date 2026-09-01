"""Run the pre-market NIFTY 500 scan and publish dashboard state."""

from pathlib import Path
import json

from bot_engine import scan_nifty500


def records(frame):
    return frame.where(frame.notna(), None).to_dict(orient="records")


def main():
    result = scan_nifty500()
    universe_rows = records(result["breadth_universe"])
    classified_rows = records(result["classified"])
    buy_rows = records(result["buy_set"])
    sell_rows = records(result["sell_set"])
    errors = result["errors"]

    # Versioned state contract consumed by the Trend Worker.  Keep these
    # validation keys at the top level so a malformed/partial scan is rejected
    # safely instead of being treated as a tradable state.
    coverage = len(universe_rows)
    health_status = "OK" if coverage >= 480 and not errors else (
        "DEGRADED" if coverage >= 480 else "LOW_COVERAGE"
    )
    state = {
        "state_schema_version": 1,
        "health": {
            "status": health_status,
            "coverage": coverage,
            "minimum_coverage": 480,
            "errors": errors,
            "generated_at": result["generated_at"],
        },
        "generated_at": result["generated_at"],
        "market": result["market"],
        "breadth_universe": universe_rows,
        "classified": classified_rows,
        "buy_set": buy_rows,
        "sell_set": sell_rows,
        "errors": errors,
    }
    path = Path("scan_state.json")
    path.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    print(f"Scan complete: universe={len(state['breadth_universe'])}, classified={len(state['classified'])}, buy={len(state['buy_set'])}, sell={len(state['sell_set'])}, errors={len(state['errors'])}")


if __name__ == "__main__":
    main()
