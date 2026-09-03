"""Dhan API trend scanner.

Dhan is the only market-data provider used by this project.
The pre-market job builds the NIFTY 500 BUY and SELL sets.
"""

from datetime import date, datetime, timedelta
from io import StringIO
import os
from zoneinfo import ZoneInfo

import pandas as pd
import requests

IST = ZoneInfo("Asia/Kolkata")
DHAN_API = "https://api.dhan.co/v2"
EQUITY_SEGMENT = "NSE_EQ"


class DhanClient:
    def __init__(self):
        self.client_id = os.getenv("DHAN_CLIENT_ID", "").strip()
        self.access_token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
        if not self.client_id or not self.access_token:
            raise RuntimeError("DHAN_CLIENT_ID or DHAN_ACCESS_TOKEN is missing")
        self.headers = {
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def post(self, path, payload):
        response = requests.post(DHAN_API + path, headers=self.headers, json=payload, timeout=60)
        if response.status_code != 200:
            raise RuntimeError(f"Dhan API {response.status_code}: {response.text[:500]}")
        return response.json()


def load_nifty500_universe():
    """Map every official NSE NIFTY 500 constituent to a Dhan NSE_EQ Security ID.

    ISIN is the primary key. If Dhan's current master has an ISIN mismatch,
    a unique NSE trading-symbol match is used as a controlled fallback.
    """
    nse = requests.get(
        "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    nse.raise_for_status()
    nifty = pd.read_csv(StringIO(nse.text))
    if len(nifty) != 500:
        raise RuntimeError(f"Official NSE NIFTY 500 file has {len(nifty)} rows; expected 500")

    master = pd.read_csv(
        "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
        low_memory=False,
    )
    nifty["ISIN Code"] = nifty["ISIN Code"].astype(str).str.upper().str.strip()
    nifty["Symbol"] = nifty["Symbol"].astype(str).str.upper().str.strip()

    equity = master[
        master["EXCH_ID"].astype(str).str.upper().eq("NSE")
        & master["SEGMENT"].astype(str).str.upper().eq("E")
        & master["INSTRUMENT"].astype(str).str.upper().eq("EQUITY")
        & master["SERIES"].astype(str).str.upper().eq("EQ")
    ].copy()
    equity["ISIN"] = equity["ISIN"].astype(str).str.upper().str.strip()

    # Primary, safest mapping: official NSE ISIN -> Dhan Security ID.
    isin_counts = equity["ISIN"].value_counts()
    unique_isin = equity[equity["ISIN"].map(isin_counts).eq(1)]
    isin_mapping = unique_isin.set_index("ISIN")["SECURITY_ID"].to_dict()
    nifty["SecurityId"] = nifty["ISIN Code"].map(isin_mapping)

    # Controlled fallback: unique NSE symbol -> Dhan symbol. Dhan's detailed
    # master has used different symbol-column names across versions, so detect
    # the available column instead of assuming one fixed schema.
    symbol_candidates = [
        "SEM_TRADING_SYMBOL", "SYMBOL_NAME", "SYMBOL", "TRADING_SYMBOL",
        "SEM_CUSTOM_SYMBOL", "SEM_SMST_SECURITY_ID",
    ]
    symbol_col = next((c for c in symbol_candidates if c in equity.columns), None)
    if symbol_col:
        equity["_SYMBOL"] = equity[symbol_col].astype(str).str.upper().str.strip()
        symbol_counts = equity["_SYMBOL"].value_counts()
        unique_symbol = equity[equity["_SYMBOL"].map(symbol_counts).eq(1)]
        symbol_mapping = unique_symbol.set_index("_SYMBOL")["SECURITY_ID"].to_dict()
        missing_mask = nifty["SecurityId"].isna()
        nifty.loc[missing_mask, "SecurityId"] = nifty.loc[missing_mask, "Symbol"].map(symbol_mapping)

    # Final controlled fallback for cases where Dhan's current master has
    # non-standard/blank SERIES or INSTRUMENT metadata for an otherwise valid
    # NSE cash-equity constituent (HFCL is one such example).
    missing_mask = nifty["SecurityId"].isna()
    broad_symbol_col = next((c for c in symbol_candidates if c in master.columns), None)
    if missing_mask.any() and broad_symbol_col:
        broad = master[master["EXCH_ID"].astype(str).str.upper().eq("NSE")].copy()
        broad["_SYMBOL"] = broad[broad_symbol_col].astype(str).str.upper().str.strip()
        broad = broad[broad["_SYMBOL"].isin(nifty.loc[missing_mask, "Symbol"])]
        if not broad.empty:
            # Prefer a single candidate; otherwise prefer rows whose segment
            # looks like cash equity. Ambiguous symbols are intentionally not mapped.
            for idx in nifty.index[missing_mask]:
                sym = nifty.at[idx, "Symbol"]
                candidates = broad[broad["_SYMBOL"].eq(sym)].copy()
                if len(candidates) > 1 and "SEGMENT" in candidates.columns:
                    eq_candidates = candidates[candidates["SEGMENT"].astype(str).str.upper().eq("E")]
                    if len(eq_candidates) == 1:
                        candidates = eq_candidates
                if len(candidates) == 1:
                    nifty.at[idx, "SecurityId"] = candidates.iloc[0]["SECURITY_ID"]

    unresolved = nifty[nifty["SecurityId"].isna()]
    if not unresolved.empty:
        details = ", ".join(
            f"{r['Symbol']} ({r['ISIN Code']})"
            for _, r in unresolved.iterrows()
        )
        raise RuntimeError(
            f"NIFTY 500 membership could not be mapped to Dhan IDs: {details}"
        )

    nifty["SecurityId"] = nifty["SecurityId"].astype(int)
    if nifty["SecurityId"].duplicated().any():
        dupes = nifty[nifty["SecurityId"].duplicated(keep=False)][["Symbol", "SecurityId"]]
        raise RuntimeError(f"Duplicate Dhan Security ID mapping in NIFTY 500: {dupes.to_dict(orient='records')}")
    if len(nifty) != 500:
        raise RuntimeError(f"NIFTY 500 membership mapped to Dhan IDs incomplete: expected 500, got {len(nifty)}")

    sector = nifty["Industry"].fillna("Other").astype(str) if "Industry" in nifty.columns else "Other"
    return pd.DataFrame({
        "Company": nifty["Company Name"].astype(str),
        "Symbol": nifty["Symbol"],
        "Sector": sector,
        "SecurityId": nifty["SecurityId"],
    })

@st.cache_data(ttl=10, show_spinner=False)
def _history(client, security_id):
    end = date.today()
    start = end - timedelta(days=420)
    payload = {
        "securityId": str(int(security_id)),
        "exchangeSegment": EQUITY_SEGMENT,
        "instrument": "EQUITY",
        "expiryCode": 0,
        "oi": False,
        "fromDate": start.isoformat(),
        "toDate": end.isoformat(),
    }
    data = client.post("/charts/historical", payload)
    data = data.get("data", data)
    if not isinstance(data, dict):
        return pd.DataFrame()
    frame = pd.DataFrame({
        "timestamp": data.get("timestamp", []),
        "open": data.get("open", []),
        "high": data.get("high", []),
        "low": data.get("low", []),
        "close": data.get("close", []),
    })
    if frame.empty:
        return frame
    # Convert timestamps to IST dates and keep only completed trading days.
    # This prevents an in-progress today's candle from being mistaken for PDC.
    frame["date"] = pd.to_datetime(frame["timestamp"], unit="s", utc=True, errors="coerce").dt.tz_convert(IST).dt.normalize().dt.tz_localize(None)
    for col in ("open", "high", "low", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    frame = frame.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    today_ist = datetime.now(IST).date()
    frame = frame[frame["date"].dt.date < today_ist].copy()
    return frame.reset_index(drop=True)


def _return_from_pdc(frame, pdc, days):
    """Return from a historical close to the previous completed day's close (PDC)."""
    latest_date = frame.iloc[-1]["date"]
    prior = frame[frame["date"] <= latest_date - pd.Timedelta(days=days)]
    if prior.empty:
        raise ValueError(f"Insufficient history for {days} days")
    base = float(prior.iloc[-1]["close"])
    if base <= 0 or pdc <= 0:
        raise ValueError("Invalid historical close/PDC")
    return (pdc - base) / base * 100.0


def get_stock_trends_from_history(frame):
    """Calculate all trend metrics from one completed Dhan history frame."""
    if len(frame) < 2:
        raise ValueError("No Dhan daily history")

    # The latest completed daily candle is PDC. Use it as the stable
    # reference for 1Y/6M/1M/1W so pre-market classification never depends
    # on today's incomplete/live candle.
    pdc = float(frame.iloc[-1]["close"])
    previous_close = float(frame.iloc[-2]["close"])
    return {
        "PDC": pdc,
        "1Y Return %": _return_from_pdc(frame, pdc, 365),
        "6M Return %": _return_from_pdc(frame, pdc, 182),
        "1M Return %": _return_from_pdc(frame, pdc, 30),
        "1W Return %": _return_from_pdc(frame, pdc, 7),
        "1D Return %": ((pdc - previous_close) / previous_close * 100.0),
    }


def get_stock_trends(client, stock):
    frame = _history(client, stock["SecurityId"])
    return get_stock_trends_from_history(frame)


def calculate_advance_decline(frame):
    """Market breadth from Dhan historical closes for the NIFTY 500 universe."""
    one_day = pd.to_numeric(frame["1D Return %"], errors="coerce")
    advances = int((one_day > 0).sum())
    declines = int((one_day < 0).sum())
    ad_ratio = advances / declines if declines else (float("inf") if advances else 1.0)
    return advances, declines, ad_ratio


def _norm_index_name(value):
    return " ".join(
        str(value).upper().replace("&", " AND ").replace("-", " ").replace("_", " ").replace("/", " ").split()
    )


def load_dhan_broad_index_ids():
    """Resolve the five required broad-market index Security IDs from Dhan's master."""
    master = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master-detailed.csv", low_memory=False)
    if "SECURITY_ID" not in master.columns:
        raise RuntimeError("Dhan security master missing SECURITY_ID")
    name_cols = [x for x in ["DISPLAY_NAME", "UNDERLYING_SYMBOL", "SEM_TRADING_SYMBOL", "SM_SYMBOL_NAME", "SYMBOL_NAME"] if x in master.columns]
    aliases = {
        "Nifty 50": ["NIFTY 50"],
        "Nifty Next 50": ["NIFTY NEXT 50", "NIFTY NEXT50"],
        "Nifty Midcap 150": ["NIFTY MIDCAP 150", "NIFTY MIDCAP150"],
        "Nifty Smallcap 250": ["NIFTY SMALLCAP 250", "NIFTY SMALLCAP250"],
        "Nifty 500": ["NIFTY 500"],
    }
    normalized = {col: master[col].map(_norm_index_name) for col in name_cols}
    out = {}
    for label, choices in aliases.items():
        hit = None
        for choice in choices:
            choice = _norm_index_name(choice)
            mask = pd.Series(False, index=master.index)
            for col in name_cols:
                mask = mask | normalized[col].eq(choice)
            rows = master[mask]
            if len(rows):
                # Prefer an explicit index segment when the Dhan master provides it.
                segment_cols = [x for x in ["SEM_SEGMENT", "EXCHANGE_SEGMENT", "INSTRUMENT", "INSTRUMENT_TYPE"] if x in rows.columns]
                if segment_cols:
                    index_mask = pd.Series(False, index=rows.index)
                    for col in segment_cols:
                        values = rows[col].astype(str).str.upper()
                        index_mask = index_mask | values.str.contains("IDX|INDEX", regex=True, na=False)
                    index_rows = rows[index_mask]
                    if len(index_rows):
                        rows = index_rows
                if len(rows) != 1:
                    raise RuntimeError(f"Ambiguous Dhan index match for {label}; refusing to guess")
                hit = rows.iloc[0]
                break
        if hit is None:
            raise RuntimeError(f"Dhan index security ID not found for {label}")
        out[label] = int(hit["SECURITY_ID"])
    return out


def fetch_broad_market_indices(client):
    """Fetch live LTP and previous close for all mandatory broad indices from Dhan."""
    ids = load_dhan_broad_index_ids()
    data = client.post("/marketfeed/ohlc", {"IDX_I": list(ids.values())})
    quotes = ((data.get("data") or {}).get("IDX_I") or {})
    result = {}
    for name, sid in ids.items():
        q = quotes.get(str(sid), quotes.get(sid, {}))
        ltp = q.get("last_price")
        pdc = q.get("previous_close") or (q.get("ohlc") or {}).get("close")
        if ltp is None or pdc in (None, 0):
            raise RuntimeError(f"Dhan did not return LTP/PDC for {name}")
        ltp, pdc = float(ltp), float(pdc)
        result[name] = {"security_id": sid, "ltp": ltp, "pdc": pdc, "day_pct": (ltp - pdc) / pdc * 100.0}
    return result

def fetch_equity_ohlc(client, security_ids):
    ids = [int(x) for x in security_ids]
    if not ids:
        return {}
    data = client.post("/marketfeed/ohlc", {"NSE_EQ": ids})
    quotes = ((data.get("data") or {}).get("NSE_EQ") or {})
    result = {}
    for sid in ids:
        q = quotes.get(str(sid), quotes.get(sid, {}))
        ohlc = q.get("ohlc") or {}
        ltp = q.get("last_price")
        if ltp is None:
            continue
        result[int(sid)] = {
            "ltp": float(ltp),
            "open": float(ohlc.get("open", ltp)),
            "high": float(ohlc.get("high", ltp)),
            "low": float(ohlc.get("low", ltp)),
        }
    return result


def add_s1_levels(frame, history_by_sid=None):
    """Attach PDH/PDL/PDC without shrinking the universe.

    Reuse the historical frame already fetched during trend classification so
    the pre-market scan does not make a second historical API call per stock.
    """
    out = frame.copy()
    client = None if history_by_sid is not None else DhanClient()
    ranges = {}
    history_by_sid = history_by_sid or {}
    for _, stock in out.iterrows():
        try:
            sid = int(stock["SecurityId"])
            hist = history_by_sid.get(sid)
            if hist is None:
                hist = _history(client, sid)
            if len(hist) >= 1:
                # _history already excludes today's incomplete candle, so the
                # latest row is exactly the previous completed trading day.
                prev = hist.iloc[-1]
                ranges[int(stock["SecurityId"])] = {
                    "PDH": float(prev["high"]),
                    "PDL": float(prev["low"]),
                    "PDC": float(prev["close"]),
                }
        except Exception:
            continue
    for col in ("PDH", "PDL", "PDC"):
        out[col] = out["SecurityId"].map(lambda sid: ranges.get(int(sid), {}).get(col))
    return out


def scan_nifty500():
    client = DhanClient()
    universe = load_nifty500_universe()
    # Mapping coverage is allowed to be below 500 when a current Dhan master
    # record is temporarily unavailable. The same 480-stock safety threshold
    # used by the live bot applies to the pre-market scan.
    if len(universe) < 480:
        raise RuntimeError(
            f"NIFTY 500 universe coverage too low for scan: expected at least 480 mapped stocks; got {len(universe)}"
        )
    universe = universe.drop_duplicates(subset=["SecurityId"]).copy()
    if len(universe) < 480:
        raise RuntimeError(
            f"NIFTY 500 universe has insufficient unique Security IDs: expected at least 480, got {len(universe)}"
        )
    rows, errors, history_by_sid = [], [], {}

    for _, stock in universe.iterrows():
        try:
            sid = int(stock["SecurityId"])
            hist = _history(client, sid)
            trends = get_stock_trends_from_history(hist)
            history_by_sid[sid] = hist
            rows.append({**stock.to_dict(), **trends})
        except Exception as exc:
            errors.append(f"{stock['Symbol']}: {exc}")

    if not rows:
        raise RuntimeError("No stock trends could be calculated")

    frame = pd.DataFrame(rows)
    frame = add_s1_levels(frame, history_by_sid)
    # A stock can be in breadth but must have complete Dhan historical levels
    # before it is eligible for any strategy set.
    strategy_frame = frame.dropna(subset=["PDH", "PDL", "PDC"]).copy()
    periods = ["1Y Return %", "6M Return %", "1M Return %", "1W Return %"]
    strategy_frame["Trend"] = "MIXED"
    strategy_frame.loc[(strategy_frame[periods] > 0).all(axis=1), "Trend"] = "BULLISH"
    strategy_frame.loc[(strategy_frame[periods] < 0).all(axis=1), "Trend"] = "BEARISH"

    advances, declines, ad_ratio = calculate_advance_decline(frame)
    index_basis = fetch_broad_market_indices(client)
    nifty500 = index_basis["Nifty 500"]
    ltp, pdc, day_pct, nifty_security_id = nifty500["ltp"], nifty500["pdc"], nifty500["day_pct"], nifty500["security_id"]
    all_indices_buy = all(float(v["day_pct"]) > 0 for v in index_basis.values())
    all_indices_sell = all(float(v["day_pct"]) < 0 for v in index_basis.values())
    if all_indices_buy and ad_ratio > 1:
        mode = "BUY"
    elif all_indices_sell and ad_ratio < 1:
        mode = "SELL"
    else:
        mode = "NEUTRAL"
    return {
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "market": {
            "ltp": ltp,
            "pdc": pdc,
            "day_pct": day_pct,
            "advances": advances,
            "declines": declines,
            "ad_ratio": ad_ratio,
            "mode": mode,
            "index_basis": index_basis,
            "market_basis_status": "FULL BUY ALIGNMENT" if mode == "BUY" else ("FULL SELL ALIGNMENT" if mode == "SELL" else "NOT ALIGNED — NO NEW TRADES"),
            "security_id": nifty_security_id,
        },
        # Keep the complete 500-member universe separate from the scan-success
        # frame. Historical scan failures must never shrink live A/D coverage.
        "breadth_universe": universe.copy(),
        "classified": strategy_frame,
        "buy_set": strategy_frame[strategy_frame["Trend"].eq("BULLISH")].copy(),
        "sell_set": strategy_frame[strategy_frame["Trend"].eq("BEARISH")].copy(),
        "errors": errors,
    }
