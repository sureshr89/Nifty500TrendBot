"""Dhan-only NIFTY 500 multi-timeframe trend scanner.

This phase classifies stocks only. It does not place or simulate trades.
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
    def __init__(self) -> None:
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

    def post(self, path: str, payload: dict) -> dict:
        response = requests.post(
            DHAN_API + path,
            headers=self.headers,
            json=payload,
            timeout=60,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Dhan API {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        if isinstance(data, dict) and data.get("status") == "failure":
            raise RuntimeError(str(data))
        return data


def load_dhan_nifty500() -> pd.DataFrame:
    """Load NIFTY 500 membership and map every stock to Dhan security ID."""
    response = requests.get(
        "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()
    nifty = pd.read_csv(StringIO(response.text))

    required = ["Company Name", "Symbol", "ISIN Code"]
    missing = [column for column in required if column not in nifty.columns]
    if missing:
        raise RuntimeError(f"NIFTY 500 columns missing: {missing}")

    master = pd.read_csv(
        "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
        low_memory=False,
    )
    for column in ["EXCH_ID", "SEGMENT", "INSTRUMENT", "SERIES", "ISIN", "SECURITY_ID"]:
        if column not in master.columns:
            raise RuntimeError(f"Dhan master column missing: {column}")

    nifty["ISIN Code"] = nifty["ISIN Code"].astype(str).str.upper().str.strip()
    equity = master[
        master["EXCH_ID"].astype(str).str.upper().eq("NSE")
        & master["SEGMENT"].astype(str).str.upper().eq("E")
        & master["INSTRUMENT"].astype(str).str.upper().eq("EQUITY")
        & master["SERIES"].astype(str).str.upper().eq("EQ")
    ].copy()
    equity["ISIN"] = equity["ISIN"].astype(str).str.upper().str.strip()

    mapping = (
        equity.drop_duplicates("ISIN")
        .set_index("ISIN")["SECURITY_ID"]
        .to_dict()
    )
    nifty["SecurityId"] = nifty["ISIN Code"].map(mapping)
    nifty = nifty.dropna(subset=["SecurityId"]).copy()
    nifty["SecurityId"] = nifty["SecurityId"].astype(int)

    if len(nifty) != 500:
        raise RuntimeError(
            f"NIFTY 500 mapping incomplete: expected 500, got {len(nifty)}"
        )

    nifty["Symbol"] = nifty["Symbol"].astype(str).str.upper().str.strip()
    sector_column = "Industry" if "Industry" in nifty.columns else None
    nifty["Sector"] = (
        nifty[sector_column].fillna("Other").astype(str)
        if sector_column
        else "Other"
    )
    return nifty[["Company Name", "Symbol", "Sector", "SecurityId"]].copy()


def _history_dataframe(client: DhanClient, security_id: int, interval: str) -> pd.DataFrame:
    end_date = date.today()
    start_date = end_date - timedelta(days=420)
    payload = {
        "securityId": str(security_id),
        "exchangeSegment": EQUITY_SEGMENT,
        "instrument": "EQUITY",
        "expiryCode": 0,
        "oi": False,
        "fromDate": start_date.isoformat(),
        "toDate": end_date.isoformat(),
        "interval": interval,
    }
    data = client.post("/charts/historical", payload)
    data = data.get("data", data)
    if not isinstance(data, dict) or "timestamp" not in data:
        return pd.DataFrame()

    frame = pd.DataFrame(
        {
            "timestamp": data.get("timestamp", []),
            "close": data.get("close", []),
        }
    )
    if frame.empty:
        return frame

    frame["date"] = pd.to_datetime(
        frame["timestamp"], unit="s", errors="coerce"
    )
    frame["close"] = pd.to_numeric(frame["close"], errors="coerce")
    frame = frame.dropna(subset=["date", "close"])
    frame = frame[frame["date"].dt.date <= end_date]
    return frame.sort_values("date").reset_index(drop=True)


def _return_over_days(frame: pd.DataFrame, days: int) -> float:
    if frame.empty or len(frame) < 2:
        raise ValueError("Insufficient historical data")
    latest = float(frame.iloc[-1]["close"])
    target_date = frame.iloc[-1]["date"] - pd.Timedelta(days=days)
    prior = frame[frame["date"] <= target_date]
    if prior.empty:
        raise ValueError(f"Insufficient data for {days}-day return")
    base = float(prior.iloc[-1]["close"])
    if base <= 0:
        raise ValueError("Invalid historical close")
    return (latest - base) / base * 100.0


def get_stock_trends(client: DhanClient, stock: pd.Series) -> dict:
    """Calculate four requested timeframe returns from Dhan historical data."""
    security_id = int(stock["SecurityId"])
    daily = _history_dataframe(client, security_id, "D")
    if daily.empty:
        raise ValueError("No Dhan daily history")

    latest = float(daily.iloc[-1]["close"])
    return {
        "1Y Return %": _return_over_days(daily, 365),
        "6M Return %": _return_over_days(daily, 182),
        "1M Return %": _return_over_days(daily, 30),
        "1W Return %": _return_over_days(daily, 7),
        "LatestClose": latest,
    }


def fetch_nifty500_index(client: DhanClient) -> tuple[float, float]:
    """Fetch NIFTY 500 LTP and previous close from Dhan index quotes."""
    master = pd.read_csv(
        "https://images.dhan.co/api-data/api-scrip-master-detailed.csv",
        low_memory=False,
    )
    index_rows = master[
        master["DISPLAY_NAME"].astype(str).str.upper().eq("NIFTY 500")
        & master["SEGMENT"].astype(str).str.upper().eq("I")
    ].copy()
    if index_rows.empty or "SECURITY_ID" not in index_rows.columns:
        raise RuntimeError("Dhan NIFTY 500 index security ID not found")

    security_id = int(index_rows.iloc[0]["SECURITY_ID"])
    response = client.post(
        "/marketfeed/ohlc",
        {"IDX_I": [security_id]},
    )
    quotes = (response.get("data") or {}).get("IDX_I", {})
    quote = quotes.get(str(security_id), {}) or {}
    ltp = quote.get("last_price")
    ohlc = quote.get("ohlc") or {}
    pdc = quote.get("previous_close") or ohlc.get("close")
    if ltp is None or pdc is None:
        raise RuntimeError("Dhan did not return NIFTY 500 LTP/PDC")
    return float(ltp), float(pdc)


def scan_nifty500() -> dict:
    client = DhanClient()
    universe = load_dhan_nifty500()
    ltp, pdc = fetch_nifty500_index(client)

    rows = []
    errors = []
    for _, stock in universe.iterrows():
        try:
            trends = get_stock_trends(client, stock)
            rows.append({**stock.to_dict(), **trends})
        except Exception as exc:
            errors.append(f"{stock['Symbol']}: {exc}")

    if not rows:
        raise RuntimeError("No NIFTY 500 historical data could be read from Dhan")

    frame = pd.DataFrame(rows)
    timeframe_columns = [
        "1Y Return %",
        "6M Return %",
        "1M Return %",
        "1W Return %",
    ]
    frame["Trend"] = "MIXED"
    frame.loc[(frame[timeframe_columns] > 0).all(axis=1), "Trend"] = "BULLISH"
    frame.loc[(frame[timeframe_columns] < 0).all(axis=1), "Trend"] = "BEARISH"

    buy_set = frame[frame["Trend"].eq("BULLISH")].copy()
    sell_set = frame[frame["Trend"].eq("BEARISH")].copy()
    mode = "BUY" if ltp > pdc else "SELL" if ltp < pdc else "NEUTRAL"

    return {
        "generated_at": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST"),
        "market": {
            "ltp": ltp,
            "pdc": pdc,
            "day_pct": (ltp - pdc) / pdc * 100.0,
            "mode": mode,
        },
        "classified": frame,
        "buy_set": buy_set,
        "sell_set": sell_set,
        "errors": errors,
    }
