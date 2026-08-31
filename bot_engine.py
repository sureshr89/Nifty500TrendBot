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
    # Membership file is used only to define the NIFTY 500 universe.
    response = requests.get(
        "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()
    nifty = pd.read_csv(StringIO(response.text))
    master = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master-detailed.csv", low_memory=False)

    nifty["ISIN Code"] = nifty["ISIN Code"].astype(str).str.upper().str.strip()
    equity = master[
        master["EXCH_ID"].astype(str).str.upper().eq("NSE")
        & master["SEGMENT"].astype(str).str.upper().eq("E")
        & master["INSTRUMENT"].astype(str).str.upper().eq("EQUITY")
        & master["SERIES"].astype(str).str.upper().eq("EQ")
    ].copy()
    equity["ISIN"] = equity["ISIN"].astype(str).str.upper().str.strip()
    mapping = equity.drop_duplicates("ISIN").set_index("ISIN")["SECURITY_ID"].to_dict()
    nifty["SecurityId"] = nifty["ISIN Code"].map(mapping)
    nifty = nifty.dropna(subset=["SecurityId"]).copy()
    nifty["SecurityId"] = nifty["SecurityId"].astype(int)
    if len(nifty) != 500:
        raise RuntimeError(f"NIFTY 500 mapping incomplete: expected 500, got {len(nifty)}")

    nifty["Symbol"] = nifty["Symbol"].astype(str).str.upper().str.strip()
    sector = nifty["Industry"].fillna("Other").astype(str) if "Industry" in nifty.columns else "Other"
    return pd.DataFrame({
        "Company": nifty["Company Name"].astype(str),
        "Symbol": nifty["Symbol"],
        "Sector": sector,
        "SecurityId": nifty["SecurityId"],
    })


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
        "interval": "D",
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
    frame["date"] = pd.to_datetime(frame["timestamp"], unit="s", errors="coerce")
    for col in ("open", "high", "low", "close"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    return frame.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)


def _return(frame, days):
    latest_date = frame.iloc[-1]["date"]
    prior = frame[frame["date"] <= latest_date - pd.Timedelta(days=days)]
    if prior.empty:
        raise ValueError(f"Insufficient history for {days} days")
    base = float(prior.iloc[-1]["close"])
    latest = float(frame.iloc[-1]["close"])
    if base <= 0:
        raise ValueError("Invalid historical close")
    return (latest - base) / base * 100.0


def get_stock_trends(client, stock):
    frame = _history(client, stock["SecurityId"])
    if len(frame) < 2:
        raise ValueError("No Dhan daily history")
    return {
        "1Y Return %": _return(frame, 365),
        "6M Return %": _return(frame, 182),
        "1M Return %": _return(frame, 30),
        "1W Return %": _return(frame, 7),
        "1D Return %": ((float(frame.iloc[-1]["close"]) - float(frame.iloc[-2]["close"])) / float(frame.iloc[-2]["close"]) * 100.0),
    }


def calculate_advance_decline(frame):
    """Market breadth from Dhan historical closes for the NIFTY 500 universe."""
    one_day = pd.to_numeric(frame["1D Return %"], errors="coerce")
    advances = int((one_day > 0).sum())
    declines = int((one_day < 0).sum())
    ad_ratio = advances / declines if declines else (float("inf") if advances else 1.0)
    return advances, declines, ad_ratio


def fetch_nifty500_index(client):
    master = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master-detailed.csv", low_memory=False)
    rows = master[
        master["DISPLAY_NAME"].astype(str).str.upper().eq("NIFTY 500")
        & master["SEGMENT"].astype(str).str.upper().eq("I")
    ]
    if rows.empty:
        raise RuntimeError("Dhan NIFTY 500 index security ID not found")
    security_id = int(rows.iloc[0]["SECURITY_ID"])
    data = client.post("/marketfeed/ohlc", {"IDX_I": [security_id]})
    quote = ((data.get("data") or {}).get("IDX_I") or {}).get(str(security_id), {})
    ltp = quote.get("last_price")
    pdc = quote.get("previous_close") or (quote.get("ohlc") or {}).get("close")
    if ltp is None or pdc is None:
        raise RuntimeError("Dhan did not return NIFTY 500 LTP/PDC")
    return float(ltp), float(pdc), security_id


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


def add_s1_levels(frame):
    out = frame.copy()
    pdh, pdl = [], []
    client = DhanClient()
    for _, stock in out.iterrows():
        hist = _history(client, stock["SecurityId"])
        if len(hist) < 2:
            pdh.append(None); pdl.append(None)
        else:
            prev = hist.iloc[-2]
            pdh.append(float(prev["high"]))
            pdl.append(float(prev["low"]))
    out["PDH"] = pdh
    out["PDL"] = pdl
    return out.dropna(subset=["PDH", "PDL"]).copy()


def scan_nifty500():
    client = DhanClient()
    universe = load_nifty500_universe()
    rows, errors = [], []

    for _, stock in universe.iterrows():
        try:
            rows.append({**stock.to_dict(), **get_stock_trends(client, stock)})
        except Exception as exc:
            errors.append(f"{stock['Symbol']}: {exc}")

    if not rows:
        raise RuntimeError("No stock trends could be calculated")

    frame = pd.DataFrame(rows)
    frame = add_s1_levels(frame)
    periods = ["1Y Return %", "6M Return %", "1M Return %", "1W Return %"]
    frame["Trend"] = "MIXED"
    frame.loc[(frame[periods] > 0).all(axis=1), "Trend"] = "BULLISH"
    frame.loc[(frame[periods] < 0).all(axis=1), "Trend"] = "BEARISH"

    advances, declines, ad_ratio = calculate_advance_decline(frame)
    ltp, pdc, nifty_security_id = fetch_nifty500_index(client)
    day_pct = ((ltp - pdc) / pdc * 100.0) if pdc else 0.0
    if day_pct > 0 and ad_ratio > 1:
        mode = "BUY"
    elif day_pct < 0 and ad_ratio < 1:
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
            "security_id": nifty_security_id,
        },
        "classified": frame,
        "buy_set": frame[frame["Trend"].eq("BULLISH")].copy(),
        "sell_set": frame[frame["Trend"].eq("BEARISH")].copy(),
        "errors": errors,
    }
