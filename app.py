"""Active Streamlit dashboard and 15-second S1 paper-trading worker.

Pre-market BUY/SELL sets and PDH/PDL are read from the bot-state branch.
When the Streamlit app is active, this app fetches fresh Dhan market data every
15 seconds and evaluates the finalized S1 paper-trading strategy.
"""

import math
import json
import time
import base64
from datetime import datetime
from io import StringIO
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

IST = ZoneInfo("Asia/Kolkata")
REPO = "sureshr89/Nifty500TrendBot"
STATE_URL = f"https://raw.githubusercontent.com/{REPO}/bot-state/scan_state.json"
STATE_URL_CACHE_BUST = f"https://raw.githubusercontent.com/{REPO}/bot-state/scan_state.json"
STRATEGY_STATE_VERSION = "S1_PDH_PDL_RR125_V4"

APP_BUILD = "mandatory-5index-basis-v6-dhan-master-fix"

st.set_page_config(page_title="NIFTY 500 Trend Bot", page_icon="📈", layout="wide")
st_autorefresh(interval=15_000, key="trend_dashboard_refresh")

@st.cache_data(ttl=3600, show_spinner=False)
def load_full_nifty500_universe():
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
    }).to_dict(orient="records")

@st.cache_data(ttl=10, show_spinner=False)
def load_premarket_state():
    response = requests.get(STATE_URL, params={"t": int(time.time() // 10)}, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Invalid state format")
    return data

class DhanLiveClient:
    def __init__(self):
        client_id = st.secrets.get("DHAN_CLIENT_ID")
        access_token = st.secrets.get("DHAN_ACCESS_TOKEN")
        if not client_id or not access_token:
            raise RuntimeError("Add DHAN_CLIENT_ID and DHAN_ACCESS_TOKEN to Streamlit Secrets.")
        self.headers = {
            "access-token": str(access_token),
            "client-id": str(client_id),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def post(self, path, payload):
        url = "https://api.dhan.co/v2" + path
        for attempt in range(3):
            response = requests.post(url, headers=self.headers, json=payload, timeout=15)
            if response.status_code == 429 and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            response.raise_for_status()
            return response.json()

    def quotes(self, security_ids, exchange_segment="NSE_EQ"):
        # Dhan expects numeric security IDs and the correct exchange-segment key.
        # A valid Dhan ID can occasionally be omitted from a large quote response,
        # so retry only missing IDs before declaring NO LTP.
        ids = list(dict.fromkeys(int(x) for x in security_ids))
        if not ids:
            return {}

        def parse(data, requested):
            rows = ((data.get("data") or {}).get(exchange_segment) or {})
            out = {}
            for sid in requested:
                row = rows.get(str(sid), rows.get(sid, {}))
                ohlc = row.get("ohlc") or {}
                ltp = row.get("last_price")
                if ltp in (None, ""):
                    continue
                try:
                    ltp = float(ltp)
                except (TypeError, ValueError):
                    continue
                if ltp <= 0:
                    continue
                previous_close = row.get("previous_close")
                if previous_close is None:
                    previous_close = row.get("prev_close")
                if previous_close is None:
                    previous_close = ohlc.get("previous_close")
                if previous_close is None:
                    previous_close = ohlc.get("close")
                out[int(sid)] = {
                    "ltp": ltp,
                    "open": float(ohlc.get("open", ltp)),
                    "high": float(ohlc.get("high", ltp)),
                    "low": float(ohlc.get("low", ltp)),
                    "previous_close": float(previous_close) if previous_close not in (None, "") else None,
                }
            return out

        result = parse(self.post("/marketfeed/ohlc", {exchange_segment: ids}), ids)
        missing = [sid for sid in ids if sid not in result]

        # Retry omitted IDs in smaller batches, then individually.
        for start in range(0, len(missing), 50):
            retry_ids = missing[start:start + 50]
            result.update(parse(
                self.post("/marketfeed/ohlc", {exchange_segment: retry_ids}),
                retry_ids,
            ))
        missing = [sid for sid in ids if sid not in result]
        for sid in missing:
            try:
                result.update(parse(
                    self.post("/marketfeed/ohlc", {exchange_segment: [sid]}),
                    [sid],
                ))
            except Exception:
                pass
        return result

@st.cache_data(ttl=3600, show_spinner=False)
def load_dhan_broad_index_ids():
    """Resolve required broad-market indices from Dhan's detailed security master.

    Dhan's master labels index rows differently from equity rows, so matching is
    performed across all available name columns instead of assuming EXCH_ID=NSE
    and a particular SEGMENT spelling.
    """
    master = pd.read_csv("https://images.dhan.co/api-data/api-scrip-master-detailed.csv", low_memory=False)

    def norm(x):
        return (str(x).upper()
                .replace("&", " AND ")
                .replace("-", " ")
                .replace("_", " ")
                .replace("/", " ")
                .strip())

    name_cols = [x for x in [
        "DISPLAY_NAME", "UNDERLYING_SYMBOL", "SEM_TRADING_SYMBOL",
        "SM_SYMBOL_NAME", "SYMBOL_NAME"
    ] if x in master.columns]
    if not name_cols or "SECURITY_ID" not in master.columns:
        raise RuntimeError("Dhan security master does not contain required index name/security-id columns")

    aliases = {
        "Nifty 50": ["NIFTY 50", "NIFTY"],
        "Nifty Next 50": ["NIFTY NEXT 50", "NIFTY NEXT50"],
        "Nifty Midcap 150": ["NIFTY MIDCAP 150", "NIFTY MIDCAP150"],
        "Nifty Smallcap 250": ["NIFTY SMALLCAP 250", "NIFTY SMALLCAP250"],
        "Nifty 500": ["NIFTY 500"],
    }

    # Build one normalized name per searchable column. Prefer rows explicitly
    # marked as an index, but do not discard valid Dhan rows when the master
    # uses a different exchange/segment label.
    normalized = {col: master[col].map(norm) for col in name_cols}
    out = {}
    for label, choices in aliases.items():
        hit = None
        for choice in choices:
            choice = norm(choice)
            mask = pd.Series(False, index=master.index)
            for col in name_cols:
                mask = mask | normalized[col].eq(choice)
            rows = master[mask]
            if len(rows):
                # Prefer an index-like segment/exchange when duplicates exist.
                if "SEGMENT" in rows.columns:
                    idx_rows = rows[rows["SEGMENT"].astype(str).str.upper().isin(["I", "IDX", "INDEX", "NSE_IDX"])]
                    if len(idx_rows):
                        rows = idx_rows
                hit = rows.iloc[0]
                break
        if hit is None:
            # Last-resort contains match for harmless spacing/master naming changes.
            for choice in choices:
                choice = norm(choice)
                mask = pd.Series(False, index=master.index)
                for col in name_cols:
                    mask = mask | normalized[col].str.contains(choice, regex=False, na=False)
                rows = master[mask]
                if len(rows):
                    if "SEGMENT" in rows.columns:
                        idx_rows = rows[rows["SEGMENT"].astype(str).str.upper().isin(["I", "IDX", "INDEX", "NSE_IDX"])]
                        if len(idx_rows):
                            rows = idx_rows
                    hit = rows.iloc[0]
                    break
        if hit is None:
            raise RuntimeError(f"Dhan index Security ID not found for {label} in current Dhan security master")
        out[label] = int(hit["SECURITY_ID"])
    return out

def now_ist():
    return datetime.now(IST)

TRADE_DATA_START = "09:15"

def in_entry_window(dt):
    hhmm = dt.strftime("%H:%M")
    # All new S1 trade decisions/data begin from the regular market open at 09:15 IST.
    return TRADE_DATA_START <= hhmm < "13:00"

def _github_trade_state_url():
    return f"https://api.github.com/repos/{REPO}/contents/paper_trades.json"

def load_persisted_trades():
    token = st.secrets.get("GITHUB_TOKEN")
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    response = requests.get(_github_trade_state_url(), headers=headers, params={"ref": "bot-state"}, timeout=15)
    if response.status_code == 404:
        return {"trades": [], "last_ltp": {}, "strategy_version": STRATEGY_STATE_VERSION}
    response.raise_for_status()
    payload = response.json()
    raw = base64.b64decode(payload["content"]).decode("utf-8")
    data = json.loads(raw)
    if data.get("strategy_version") != STRATEGY_STATE_VERSION:
        return {"trades": [], "last_ltp": {}, "strategy_version": STRATEGY_STATE_VERSION, "_sha": payload.get("sha")}
    return {"trades": data.get("trades", []), "last_ltp": data.get("last_ltp", {}), "strategy_version": STRATEGY_STATE_VERSION, "_sha": payload.get("sha")}

def persist_trades(runtime):
    """Persist trades without allowing a stale/empty runtime to erase history."""
    token = st.secrets.get("GITHUB_TOKEN")
    if not token:
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"}
    current = requests.get(_github_trade_state_url(), headers=headers, params={"ref": "bot-state"}, timeout=15)
    sha, stored = None, {"trades": [], "last_ltp": {}, "strategy_version": STRATEGY_STATE_VERSION}
    if current.status_code == 200:
        payload = current.json()
        sha = payload.get("sha")
        try:
            stored = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
        except Exception:
            return
    elif current.status_code != 404:
        current.raise_for_status()
    if stored.get("strategy_version") != STRATEGY_STATE_VERSION:
        existing = []
    else:
        existing = stored.get("trades", []) or []
    if runtime.get("strategy_version") != STRATEGY_STATE_VERSION:
        incoming = []
        runtime["trades"] = []
        runtime["last_ltp"] = {}
        runtime["strategy_version"] = STRATEGY_STATE_VERSION
    else:
        incoming = runtime.get("trades", []) or []
    merged = {}
    for t in existing + incoming:
        key = str(t.get("id") or "|".join([str(t.get("symbol", "")), str(t.get("side", "")), str(t.get("strategy", "")), str(t.get("entry_time", t.get("entry_at", ""))), str(t.get("entry_price", ""))]))
        merged[key] = {**merged.get(key, {}), **t}
    data = {
        "strategy_version": STRATEGY_STATE_VERSION,
        "trades": list(merged.values()),
        "last_ltp": {**(stored.get("last_ltp", {}) or {}), **(runtime.get("last_ltp", {}) or {})},
    }
    body = {"message": "Persist paper trade state (history protected)", "content": base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii"), "branch": "bot-state"}
    if sha:
        body["sha"] = sha
    response = requests.put(_github_trade_state_url(), headers=headers, json=body, timeout=15)
    response.raise_for_status()
def clean_trade_history(trades):
    """Keep only trades matching the current clean sizing rules."""
    cleaned = []
    for t in trades or []:
        try:
            entry = float(t.get("entry_price", 0) or 0)
            qty = float(t.get("quantity", 0) or 0)
            sl = float(t.get("SL"))
            # Recompute risk from the authoritative entry and SL instead of
            # trusting a potentially stale stored risk_per_share value.
            side = str(t.get("side", "")).upper()
            rps = (entry - sl) if side == "BUY" else (sl - entry)
            if rps <= 0:
                continue
            cap = entry * qty
            risk = rps * qty
            target = float(t.get("target"))
            reward = (target - entry) if t.get("side") == "BUY" else (entry - target)
            rr = reward / rps if rps > 0 else -1
            if (entry > 0 and qty > 0 and cap <= 150000.0 + 1e-6 and
                    1000.0 - 1e-6 <= risk <= 1500.0 + 1e-6 and
                    abs(rr - 1.25) < 1e-6):
                cleaned.append(t)
        except Exception:
            pass
    return cleaned

def trend_check(state):
    client = DhanLiveClient()
    market = dict(state.get("market", {}))
    buy_rows = state.get("buy_set", [])
    sell_rows = state.get("sell_set", [])

    # Breadth must always use the independently mapped official NIFTY 500.
    # Never inherit the smaller premarket classified/buy/sell state.
    universe_rows = load_full_nifty500_universe()
    # Prefer Dhan historical levels already prepared by the pre-market worker.
    # NSE data is never used for live LTP, A/D, or trade execution.
    range_by_sid = {}
    for _row in state.get("classified", []) or []:
        try:
            _sid = int(_row.get("SecurityId"))
            range_by_sid[_sid] = {
                "PDH": _row.get("PDH", _row.get("pdh")),
                "PDL": _row.get("PDL", _row.get("pdl")),
                "PDC": _row.get("PDC", _row.get("pdc")),
            }
        except (TypeError, ValueError):
            continue
    for _row in universe_rows:
        try:
            _rng = range_by_sid.get(int(_row.get("SecurityId")))
        except (TypeError, ValueError):
            _rng = None
        if _rng:
            _row["PDH"] = _rng.get("PDH")
            _row["PDL"] = _rng.get("PDL")
            _row["PDC"] = _rng.get("PDC")

 # Quote eligibility must NOT depend on PDC.  A stock can still need a live
    # LTP in the BUY/SELL tables even if its saved state has no usable PDC.
    universe_ids, pdc_by_id = [], {}
    for row in universe_rows:
        try:
            sid = int(row.get("SecurityId"))
            universe_ids.append(sid)
        except (TypeError, ValueError):
            continue
        try:
            row_pdc = float(row.get("PDC", row.get("pdc", 0)))
            if row_pdc > 0:
                pdc_by_id[sid] = row_pdc
        except (TypeError, ValueError):
            pass

    # Always include every strategy-set stock in the quote request.
    for row in buy_rows + sell_rows:
        try:
            universe_ids.append(int(row.get("SecurityId")))
        except (TypeError, ValueError):
            pass

    # ONE market scan per refresh: the full NIFTY 500 is fetched once and this
    # single quote batch is reused for market A/D, every sector A/D, BUY/SELL
    # candidate ranking, entries, exits, SL/Target checks and live P&L.
    # Dhan quote limits are handled by deterministic batches without creating
    # separate scans for separate purposes.
    unique_universe_ids = sorted(set(universe_ids))
    if len(unique_universe_ids) < 500:
        # Never silently fall back to Buy/Sell sets for breadth.
        # The worker will show the actual mapped universe and refuse new entries
        # until a complete 500-member state is available.
        st.error(f"Breadth universe incomplete: {len(unique_universe_ids)} mapped stocks loaded; expected 500.")
    live_quotes = {}
    quote_batch_size = 500
    for start in range(0, len(unique_universe_ids), quote_batch_size):
        quote_batch = unique_universe_ids[start:start + quote_batch_size]
        live_quotes.update(
            client.quotes(quote_batch, exchange_segment="NSE_EQ")
        )

    # Populate BUY/SELL table LTP from the same quote batch.
    for stock in buy_rows + sell_rows:
        try:
            q = live_quotes.get(int(stock.get("SecurityId")))
            if q:
                stock["LTP"] = q["ltp"]
                stock["DHAN_LIVE_VALID"] = bool(q.get("ltp") is not None and float(q.get("ltp") or 0) > 0)
        except (TypeError, ValueError):
            pass
    # Prefer saved PDC; otherwise use the previous-close value returned with
    # the same Dhan OHLC quote batch. This keeps A/D live even when the scan
    # state does not carry a PDC column.
    resolved_pdc = {}
    for sid, q in live_quotes.items():
        pdc = pdc_by_id.get(sid)
        if not pdc:
            try:
                pdc = float(q.get("previous_close", 0) or 0)
            except (TypeError, ValueError):
                pdc = 0
        if pdc and pdc > 0:
            resolved_pdc[sid] = pdc

    # Dhan is the market-data authority. A stock without a Dhan LTP and
    # Dhan previous-close is excluded from breadth and is never eligible for
    # strategy entry. NSE membership alone can never make a stock tradable.
    dhan_valid_ids = [
        sid for sid, q in live_quotes.items()
        if sid in resolved_pdc and q.get("ltp") is not None and float(q.get("ltp") or 0) > 0
    ]
    valid_ids = dhan_valid_ids
    valid_count = len(valid_ids)
    advances = sum(1 for sid in valid_ids if live_quotes[sid]["ltp"] > resolved_pdc[sid])
    declines = sum(1 for sid in valid_ids if live_quotes[sid]["ltp"] < resolved_pdc[sid])
    unchanged = sum(1 for sid in valid_ids if live_quotes[sid]["ltp"] == resolved_pdc[sid])

    # Live sector-wise A/D for the FULL valid NIFTY 500 breadth universe.
    # Strategy Buy/Sell candidate sets are never used as the sector A/D source.
    sector_by_id = {}
    for row in universe_rows:
        try:
            sid = int(row.get("SecurityId"))
        except (TypeError, ValueError):
            continue
        sector = str(row.get("Sector") or row.get("Industry") or "").strip()
        if sector:
            sector_by_id[sid] = sector

    sector_breadth = {}
    for sid in valid_ids:
        sector = sector_by_id.get(sid)
        if not sector:
            continue
        bucket = sector_breadth.setdefault(sector, {"advances": 0, "declines": 0, "unchanged": 0, "valid": 0})
        bucket["valid"] += 1
        if live_quotes[sid]["ltp"] > resolved_pdc[sid]:
            bucket["advances"] += 1
        elif live_quotes[sid]["ltp"] < resolved_pdc[sid]:
            bucket["declines"] += 1
        else:
            bucket["unchanged"] += 1
    for sector, bucket in sector_breadth.items():
        adv, dec = bucket["advances"], bucket["declines"]
        bucket["ad_ratio"] = (adv / dec) if dec > 0 else (float("inf") if adv > 0 else None)

    # A/D may be displayed at any coverage, but it is VALID FOR NEW TRADES
    # only when at least 480 of the 500 NIFTY 500 stocks have valid LTP + PDC.
    # Existing open positions are monitored independently below.
    breadth_valid = valid_count >= 480
    if declines > 0:
        ad_ratio = advances / declines
    elif advances > 0:
        ad_ratio = float("inf")
    else:
        ad_ratio = None
    # Mandatory broad-market basis. All five indices are fetched from Dhan.
    index_ids = load_dhan_broad_index_ids()
    index_quotes = client.quotes(list(index_ids.values()), exchange_segment="IDX_I")
    index_basis = {}
    for name, sid in index_ids.items():
        q = index_quotes.get(int(sid), {})
        idx_ltp, idx_pdc = q.get("ltp"), q.get("previous_close")
        pct = None
        if idx_ltp is not None and idx_pdc not in (None, 0):
            pct = (float(idx_ltp) - float(idx_pdc)) / float(idx_pdc) * 100
        index_basis[name] = {"security_id": int(sid), "ltp": idx_ltp, "pdc": idx_pdc, "pct": pct}

    nifty500_basis = index_basis["Nifty 500"]
    ltp, pdc, day_pct = nifty500_basis.get("ltp"), nifty500_basis.get("pdc"), nifty500_basis.get("pct")
    all_index_data_valid = all(v.get("pct") is not None for v in index_basis.values())
    all_indices_buy = all_index_data_valid and all(float(v["pct"]) > 0 for v in index_basis.values())
    all_indices_sell = all_index_data_valid and all(float(v["pct"]) < 0 for v in index_basis.values())
    breadth_buy = ad_ratio is not None and ad_ratio > 1
    breadth_sell = ad_ratio is not None and ad_ratio < 1
    if breadth_valid and all_indices_buy and breadth_buy:
        mode = "BUY"
    elif breadth_valid and all_indices_sell and breadth_sell:
        mode = "SELL"
    else:
        mode = "NEUTRAL"
    market_basis_status = "FULL BUY ALIGNMENT" if mode == "BUY" else ("FULL SELL ALIGNMENT" if mode == "SELL" else "NOT ALIGNED — NO NEW TRADES")
    # Coverage must be measured against UNIQUE official NIFTY 500 Security IDs.
    # universe_ids can also contain strategy rows, so never use its raw length.
    # Coverage metrics must describe the official NIFTY 500 only. Strategy
    # rows may contain duplicates and must never inflate the denominator above 500.
    coverage_ids = sorted({
        int(row.get("SecurityId"))
        for row in universe_rows
        if row.get("SecurityId") not in (None, "")
    })
    dhan_ltp_valid = sum(
        1 for _sid in coverage_ids
        if (_sid in live_quotes and live_quotes[_sid].get("ltp") is not None and float(live_quotes[_sid].get("ltp") or 0) > 0)
    )
    dhan_ltp_missing = max(0, len(coverage_ids) - dhan_ltp_valid)
    # Keep the exact NSE identity for every Dhan quote failure so a missing
    # LTP can never be hidden behind a simple count such as 499/500.
    dhan_missing_details = []
    for row in universe_rows:
        try:
            sid = int(row.get("SecurityId"))
        except (TypeError, ValueError):
            continue
        if sid in coverage_ids and sid not in live_quotes:
            dhan_missing_details.append({
                "Symbol": str(row.get("Symbol", "")),
                "Company": str(row.get("Company", "")),
                "SecurityId": sid,
                "Reason": "Dhan returned no valid LTP after batch and individual retries",
            })
    dhan_pdc_valid = sum(1 for _sid in coverage_ids if _sid in resolved_pdc and resolved_pdc[_sid] is not None)
    dhan_ad_valid = valid_count
    market.update({
        "ltp": ltp, "day_pct": day_pct, "ad_ratio": ad_ratio,
        "advances": advances, "declines": declines, "unchanged": unchanged,
        "valid_breadth_stocks": valid_count, "breadth_minimum": 480,
        "breadth_valid": breadth_valid, "sector_breadth": sector_breadth,
        "index_basis": index_basis,
        "market_basis_status": market_basis_status,
        "all_indices_buy": all_indices_buy,
        "all_indices_sell": all_indices_sell,
        "mode": mode,
        "dhan_universe_total": len(coverage_ids),
        "dhan_ltp_valid": dhan_ltp_valid,
        "dhan_ltp_missing": dhan_ltp_missing,
        "dhan_missing_details": dhan_missing_details,
        "dhan_pdc_valid": dhan_pdc_valid,
        "dhan_ad_valid": dhan_ad_valid,
        # Expose the same universe and quote snapshot used by the live scan.
        # The dashboard must not rebuild the universe independently.
        "breadth_universe_rows": universe_rows,
        "live_quotes": live_quotes,
        "resolved_pdc": resolved_pdc,
        "dhan_quote_coverage_pct": (dhan_ltp_valid / len(coverage_ids) * 100) if coverage_ids else 0,
    })

    if "trend_runtime" not in st.session_state:
        persisted = load_persisted_trades()
        persisted = persisted if persisted is not None else {"trades": [], "last_ltp": {}, "strategy_version": STRATEGY_STATE_VERSION}
        if persisted.get("strategy_version") != STRATEGY_STATE_VERSION:
            persisted = {"trades": [], "last_ltp": {}, "strategy_version": STRATEGY_STATE_VERSION}
        st.session_state.trend_runtime = persisted
    runtime = st.session_state.trend_runtime
    # Force any pre-finalized session runtime to start clean; prevents stale browser
    # sessions from writing old strategy trades back into the new S1 history.
    if runtime.get("strategy_version") != STRATEGY_STATE_VERSION:
        runtime["trades"] = []
        runtime["last_ltp"] = {}
        runtime["strategy_version"] = STRATEGY_STATE_VERSION
    # Backfill older stored trades created before the 1D snapshot column was
    # added. Match by SecurityId so existing history can display the correct
    # pre-market 1D Return % when available.
    _trend1d_by_sid = {}
    for _r in state.get("classified", []) or []:
        try:
            _sid = int(_r.get("SecurityId"))
            _v = _r.get("1D Return %", _r.get("trend_1d"))
            if _v is not None:
                _trend1d_by_sid[_sid] = _v
        except (TypeError, ValueError):
            continue
    for _t in runtime.get("trades", []) or []:
        if _t.get("trend_1d") is None:
            try:
                _sid = int(_t.get("SecurityId"))
                if _sid in _trend1d_by_sid:
                    _t["trend_1d"] = _trend1d_by_sid[_sid]
            except (TypeError, ValueError):
                pass

   
    trades = runtime["trades"]
    dt = now_ist()
    open_trades = [t for t in trades if t["status"] == "OPEN"]

    # 14:55 mandatory square-off for every open strategy position.
    if dt.strftime("%H:%M") >= "14:55":
        for trade in open_trades:
            q = live_quotes.get(int(trade["SecurityId"]))
            if q:
                trade.update({"status": "CLOSED", "exit_price": q["ltp"], "exit_reason": "AUTO_SQUARE_OFF", "exit_time": dt.strftime("%Y-%m-%d %H:%M:%S IST")})
        open_trades = [t for t in trades if t["status"] == "OPEN"]

    # SL / target monitoring for every open strategy position.
    for trade in list(open_trades):
        q = live_quotes.get(int(trade["SecurityId"]))
        if not q:
            continue
        px = q["ltp"]
        if trade["side"] == "BUY":
            reason = "STOP_LOSS" if px <= trade["SL"] else ("TARGET" if px >= trade["target"] else None)
        else:
            reason = "STOP_LOSS" if px >= trade["SL"] else ("TARGET" if px <= trade["target"] else None)
        if reason:
            configured_exit = float(trade["SL"] if reason == "STOP_LOSS" else trade["target"])
            trade.update({
                "status": "CLOSED",
                "exit_price": configured_exit,
                "trigger_ltp": float(px),
                "exit_reason": reason,
                "exit_time": dt.strftime("%Y-%m-%d %H:%M:%S IST")
            })
    open_trades = [t for t in trades if t["status"] == "OPEN"]

    # New S1 entries: maximum 4 OPEN positions at any one time.
    # There is intentionally no whole-day trade-count cap; when an open trade
    # closes, another valid S1 opportunity may be taken while the entry window
    # and all market/breadth safeguards remain satisfied.
    # Every strategy may use ONLY the matching pre-qualified BUY/SELL stock set.
    # Never allow a new entry on incomplete breadth coverage.
    # This explicit check is kept here as a second safety gate even if mode logic changes.
    MAX_OPEN_TRADES = 4
    entry_diagnostics = {
        "entry_window": in_entry_window(dt),
        "breadth_ok": bool(breadth_valid and valid_count >= 480),
        "mode": mode,
        "open_positions": len(open_trades),
        "max_open_positions": MAX_OPEN_TRADES,
        "candidates": 0,
        "sector_aligned": 0,
        "inside_range": 0,
        "cross_detected": 0,
        "sizing_rejected": 0,
        "entries_opened": 0,
    }
    if (len(open_trades) < MAX_OPEN_TRADES and in_entry_window(dt)
            and breadth_valid and valid_count >= 480
            and mode in ("BUY", "SELL")):
        candidates = buy_rows if mode == "BUY" else sell_rows
        entry_diagnostics["candidates"] = len(candidates)

        # Priority across stocks: strongest sector breadth first for BUY,
        # weakest sector breadth first for SELL.
        def candidate_sector_ad(stock):
            sector = str(stock.get("Sector") or stock.get("Industry") or "").strip()
            stats = sector_breadth.get(sector, {})
            ratio = stats.get("ad_ratio")
            if ratio is None:
                return float("-inf") if mode == "BUY" else float("inf")
            return ratio

        candidates = sorted(
            candidates,
            key=candidate_sector_ad,
            reverse=(mode == "BUY"),
        )
        quotes = live_quotes

        MIN_RISK_PER_TRADE = 1000.0
        MAX_RISK_PER_TRADE = 1500.0
        TARGET_R_MULTIPLE = 1.25  # Reward = 1.25 × initial risk (RR 1:1.25)

        def open_position(strategy, side, stock, sid, q, sl, target):
            entry = float(q["ltp"])
            risk_per_share = (entry - sl) if side == "BUY" else (sl - entry)
            if risk_per_share <= 0:
                return False

            # Position size must satisfy BOTH the SL-risk band and the
            # maximum capital deployed per trade.
            MAX_CAPITAL_PER_TRADE = 150000.0
            # Find a whole-share quantity that simultaneously satisfies the
            # mandatory ₹1,000–₹1,500 SL-risk band and the capital cap.
            # Start at the minimum quantity required for ₹1,000 risk rather
            # than rounding down from the ₹1,500 maximum.
            min_risk_qty = math.ceil(MIN_RISK_PER_TRADE / risk_per_share)
            max_risk_qty = math.floor(MAX_RISK_PER_TRADE / risk_per_share)
            capital_qty = math.floor(MAX_CAPITAL_PER_TRADE / entry)
            quantity = min(max_risk_qty, capital_qty)

            if quantity < min_risk_qty or quantity < 1:
                return False

            actual_risk = quantity * risk_per_share
            actual_capital = quantity * entry

            # Final hard safety check: absolutely no trade outside the risk
            # band or capital limit may be stored.
            if not (MIN_RISK_PER_TRADE <= actual_risk <= MAX_RISK_PER_TRADE):
                return False
            if actual_capital > MAX_CAPITAL_PER_TRADE:
                return False
            trades.append({
                "strategy": strategy, "side": side, "status": "OPEN",
                "Symbol": stock["Symbol"], "SecurityId": sid,
                "Sector": str(stock.get("Sector") or stock.get("Industry") or "").strip(),
                "sector_ad": candidate_sector_ad(stock),
                # Store the same pre-market 1D PDC-vs-previous-PDC percentage
                # used for the reference trend display.
                "trend_1d": stock.get("1D Return %", stock.get("trend_1d")),
                "entry_price": entry, "quantity": quantity,
                "risk_per_share": risk_per_share,
                "risk_amount": actual_risk,
                "PDH": float(stock["PDH"]),
                "PDL": float(stock["PDL"]), "PDC": float(stock.get("PDC", 0)),
                "SL": sl, "target": target,
                "entry_time": dt.strftime("%Y-%m-%d %H:%M:%S IST")
            })
            return True

        for stock in candidates:
            sid = int(stock["SecurityId"])
            q = quotes.get(sid)
            if not q:
                continue
            pdh, pdl, pdc = float(stock.get("PDH", 0)), float(stock.get("PDL", 0)), float(stock.get("PDC", 0))
            if pdh <= 0 or pdl <= 0 or pdc <= 0:
                continue
            # JSON persistence converts dict keys to strings, so support both
            # in-memory integer keys and persisted string keys.
            prev_ltp = runtime["last_ltp"].get(str(sid), runtime["last_ltp"].get(sid))
            sector = str(stock.get("Sector") or stock.get("Industry") or "").strip()
            sector_stats = sector_breadth.get(sector)
            sector_ad = sector_stats.get("ad_ratio") if sector_stats else None
            sector_bias_ok = (
                (mode == "BUY" and sector_ad is not None and sector_ad > 1)
                or (mode == "SELL" and sector_ad is not None and sector_ad < 1)
            )
            if not sector_bias_ok:
                runtime["last_ltp"][str(sid)] = q["ltp"]
                continue
            entry_diagnostics["sector_aligned"] += 1
            entry_made = False
            open_symbols = {
                str(t.get("Symbol", "")).upper()
                for t in trades
                if t.get("status") == "OPEN"
            }
            if str(stock.get("Symbol", "")).upper() in open_symbols:
                runtime["last_ltp"][str(sid)] = q["ltp"]
                continue

            inside_range = pdl < q["open"] < pdh
            if inside_range:
                entry_diagnostics["inside_range"] += 1
            if mode == "BUY":
                crossed = prev_ltp is not None and prev_ltp < pdh and q["ltp"] >= pdh
                if crossed:
                    entry_diagnostics["cross_detected"] += 1
                s1 = inside_range and crossed
                if s1:
                    sl=pdl; risk=q["ltp"]-sl
                    if risk>0:
                        entry_made=open_position("S1","BUY",stock,sid,q,sl,q["ltp"]+TARGET_R_MULTIPLE*risk)
            else:
                crossed = prev_ltp is not None and prev_ltp > pdl and q["ltp"] <= pdl
                if crossed:
                    entry_diagnostics["cross_detected"] += 1
                s1=inside_range and crossed
                if s1:
                    sl=pdh; risk=sl-q["ltp"]
                    if risk>0:
                        entry_made=open_position("S1","SELL",stock,sid,q,sl,q["ltp"]-TARGET_R_MULTIPLE*risk)
            if s1 and not entry_made:
                entry_diagnostics["sizing_rejected"] += 1
            if entry_made:
                entry_diagnostics["entries_opened"] += 1

            # Do not stop after the first entry. S1 allows up to four
            # simultaneous open positions, so continue evaluating candidates
            # until the limit is reached.
            if entry_made and len([t for t in trades if t.get("status") == "OPEN"]) >= MAX_OPEN_TRADES:
                runtime["last_ltp"][str(sid)] = q["ltp"]
                break
            runtime["last_ltp"][str(sid)] = q["ltp"]

    # PDH/PDL come from the existing premarket scan sets. Attach them by
    # SecurityId to the independent 500-stock breadth rows without another scan.
    range_by_id = {}
    for _row in state.get("classified", []) or []:
        try:
            _sid = int(_row.get("SecurityId"))
            range_by_id[_sid] = (_row.get("PDH", _row.get("pdh")), _row.get("PDL", _row.get("pdl")))
        except (TypeError, ValueError):
            pass
    for _row in universe_rows:
        try:
            _sid = int(_row.get("SecurityId"))
        except (TypeError, ValueError):
            continue
        _rng = range_by_id.get(_sid)
        if _rng:
            if _row.get("PDH") in (None, ""):
                _row["PDH"] = _rng[0]
            if _row.get("PDL") in (None, ""):
                _row["PDL"] = _rng[1]
    market["entry_diagnostics"] = entry_diagnostics
    persist_trades(runtime)
    return market, runtime["trades"]

# --------------------------- Dashboard ---------------------------
st.markdown("""
<style>
.block-container {max-width:1200px;padding-top:0.8rem;padding-bottom:2rem;}
div[data-testid="stExpander"] {border-radius:12px;}
.metric-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;width:100%;margin:6px 0 10px;}
.metric-card{box-sizing:border-box;background:rgba(128,128,128,.10);border:1px solid rgba(128,128,128,.20);border-radius:10px;padding:8px 7px;min-height:68px;margin:0;overflow:hidden;}
.metric-label{font-size:.68rem;opacity:.72;margin-bottom:5px;line-height:1.1;}
.metric-value{font-size:.98rem;font-weight:700;word-break:break-word;line-height:1.15;}
@media (max-width: 480px){.metric-grid{grid-template-columns:repeat(3,minmax(0,1fr));gap:5px;}.metric-card{padding:8px 6px;min-height:64px;}.metric-label{font-size:.62rem;}.metric-value{font-size:.88rem;}}
</style>
""",unsafe_allow_html=True)

st.title("📈 NIFTY 500 Trend Bot")
st.caption("Live Dhan monitoring • S1 paper strategy")

try:
    state=load_premarket_state()
    market,trades=trend_check(state)
    live_status="🟢 ACTIVE"
except Exception as exc:
    state={}; market={}; trades=[]
    live_status="🔴 ERROR"
    st.error(str(exc))

buy_rows=state.get("buy_set",[])
sell_rows=state.get("sell_set",[])
mode=market.get("mode","NEUTRAL")

def pnl(t):
    q=float(t.get("quantity",0) or 0); e=float(t.get("entry_price",0) or 0)
    px=t.get("exit_price") if t.get("status")=="CLOSED" else None
    if px is None:
        try:
            quote=market.get("live_quotes",{}).get(int(t.get("SecurityId")))
            px=quote.get("ltp") if quote else e
        except Exception:
            px=e
    px=float(px or e)
    return (px-e)*q if t.get("side")=="BUY" else (e-px)*q

def capital(t):
    return float(t.get("entry_price",0) or 0)*float(t.get("quantity",0) or 0)

def trade_df(items):
    data=[]
    for t in items:
        entry=float(t.get("entry_price",0) or 0)
        sl=float(t.get("SL",0) or 0)
        risk_per_share=abs(entry-sl)
        risk_amount=risk_per_share*float(t.get("quantity",0) or 0)
        realized=pnl(t)
        data.append({
            "Date":str(t.get("entry_time",""))[:10],
            "Strategy":t.get("strategy","—"),"Side":t.get("side","—"),
            "Symbol":t.get("Symbol","—"),
            "Stock Name":t.get("Company") or t.get("Stock Name") or t.get("Symbol","—"),
            "Sector":t.get("Sector","—"),
            # Snapshot at entry: this is the exact Sector A/D that qualified
            # and prioritized the trade, not a later live value.
            "Sector A/D at Entry":t.get("sector_ad","—"),
            # 1D trend snapshot: latest completed PDC versus the previous
            # completed PDC. Reference only; it does not change trade entry.
            "1D % at Entry":t.get("1D %", t.get("trend_1d", "—")),
            "Entry Time":t.get("entry_time","—"),"Entry":t.get("entry_price"),
            "Qty":t.get("quantity"),"Capital Used":capital(t),
            "SL":t.get("SL"),"Target":t.get("target"),
            "Risk / Share":risk_per_share,"Risk Amount":risk_amount,
            "Status":t.get("status","—"),
            "Exit Time":t.get("exit_time","—"),"Exit Price":t.get("exit_price"),
            "Trigger LTP":t.get("trigger_ltp"),"Exit Reason":t.get("exit_reason","OPEN" if t.get("status")=="OPEN" else "—"),
            "P&L":realized,
            "Return %":(realized/capital(t)*100) if capital(t)>0 else 0.0
        })
    return pd.DataFrame(data)

def stats(items):
    closed=[t for t in items if t.get("status")=="CLOSED"]
    opened=[t for t in items if t.get("status")=="OPEN"]
    cp=[pnl(t) for t in closed]
    wins=sum(x>0 for x in cp); losses=sum(x<0 for x in cp)
    caps=[capital(t) for t in items if capital(t)>0]

    # Max Capital Used means TOTAL capital deployed simultaneously, not the
    # largest single trade. Build an entry/exit exposure timeline.
    events=[]
    for idx,t in enumerate(items):
        cap=capital(t)
        if cap<=0:
            continue
        entry_time=str(t.get("entry_time",""))
        events.append((entry_time, 1, cap, idx))
        if t.get("status")=="CLOSED" and t.get("exit_time"):
            # Exit is processed after an entry at the same timestamp.
            events.append((str(t.get("exit_time","")), 0, -cap, idx))
    events.sort(key=lambda x:(x[0], -x[1], x[3]))
    running_cap=0.0
    max_deployed=0.0
    for _,_,delta,_ in events:
        running_cap += delta
        max_deployed=max(max_deployed,running_cap)

    return {
        "taken":len(items),"open":len(opened),"closed":len(closed),
        "wins":wins,"losses":losses,"winpct":wins/len(closed)*100 if closed else 0,
        "realized":sum(cp),"live":sum(pnl(t) for t in opened),
        "maxcap":max_deployed,"mincap":min(caps) if caps else 0,
        "avgwin":sum(x for x in cp if x>0)/wins if wins else 0,
        "avgloss":sum(x for x in cp if x<0)/losses if losses else 0,
        "maxprofit":max(cp) if cp else 0,"maxloss":min(cp) if cp else 0,
        "grossprofit":sum(x for x in cp if x>0),"grossloss":sum(x for x in cp if x<0),
        "profitfactor":(sum(x for x in cp if x>0)/abs(sum(x for x in cp if x<0))) if sum(x for x in cp if x<0)!=0 else 0,
        "expectancy":sum(cp)/len(cp) if cp else 0
    }

today_key=datetime.now(IST).strftime("%Y-%m-%d")
today_items=[t for t in trades if str(t.get("entry_time","")).startswith(today_key)]
tdf=trade_df(today_items)

def cards(pairs, cols=3):
    # HTML/CSS grid is used instead of st.columns because Streamlit stacks
    # columns vertically on narrow mobile screens.
    html='<div class="metric-grid">'
    for label,value in pairs:
        html += f'<div class="metric-card"><div class="metric-label">{label}</div><div class="metric-value">{value}</div></div>'
    html+='</div>'
    st.markdown(html,unsafe_allow_html=True)

# 1 LIVE MARKET
st.subheader("📊 Live Market")
basis = market.get("index_basis", {}) or {}
basis_rows = []
for _name in ["Nifty 50", "Nifty Next 50", "Nifty Midcap 150", "Nifty Smallcap 250", "Nifty 500"]:
    _v = basis.get(_name, {})
    _pct = _v.get("pct")
    _direction = "🟢 BUY" if _pct is not None and float(_pct) > 0 else ("🔴 SELL" if _pct is not None and float(_pct) < 0 else "⚪ NOT ALIGNED")
    basis_rows.append({"Index": _name, "LTP": _v.get("ltp", "—"), "Previous Close": _v.get("pdc", "—"), "% Change": "—" if _pct is None else f"{float(_pct):.2f}%", "Direction": _direction})
st.dataframe(pd.DataFrame(basis_rows), use_container_width=True, hide_index=True)
_ad = market.get("ad_ratio")
_ad_direction = "🟢 BUY" if _ad is not None and _ad > 1 else ("🔴 SELL" if _ad is not None and _ad < 1 else "⚪ NOT ALIGNED")
st.dataframe(pd.DataFrame([{"NIFTY 500 A/D Ratio": "—" if _ad is None else ("∞" if _ad == float("inf") else f"{float(_ad):.3f}"), "Advancing": market.get("advances", 0), "Declining": market.get("declines", 0), "A/D Direction": _ad_direction, "Final Market Basis": market.get("market_basis_status", "NOT ALIGNED — NO NEW TRADES"), "Trade Bias": mode}]), use_container_width=True, hide_index=True)
st.caption(
    f"Market Breadth • Advancing: {market.get('advances',0)} • "
    f"Declining: {market.get('declines',0)} • "
    f"Unchanged: {market.get('unchanged',0)} • "
    f"Stocks Used for A/D: {market.get('valid_breadth_stocks',0)} / {market.get('dhan_universe_total',500)} • "
    f"Minimum Required: {market.get('breadth_minimum',480)} • "
    f"{'VALID' if market.get('breadth_valid') else 'LOW COVERAGE — NO NEW TRADES'}"
)

# Dhan coverage is display-only and uses the exact same 15-second quote batch.
with st.expander("📡 Dhan Live Coverage — 15 Second Snapshot", expanded=False):
    cards([
        ("NIFTY 500 Universe", f"{market.get('dhan_universe_total',0)}"),
        ("Dhan LTP Pulled", f"{market.get('dhan_ltp_valid',0)}"),
        ("LTP Not Pulled", f"{market.get('dhan_ltp_missing',0)}"),
        ("LTP Coverage", f"{float(market.get('dhan_quote_coverage_pct',0) or 0):.2f}%"),
        ("Dhan PDC Available", f"{market.get('dhan_pdc_valid',0)}"),
        ("Used for A/D", f"{market.get('dhan_ad_valid',0)}"),
    ])
    st.caption(
        "This section uses the same Dhan live quote batch as A/D, Sector A/D, "
        "trade monitoring and live P&L. Missing IDs are retried in smaller batches and individually."
    )
    _missing = market.get("dhan_missing_details", []) or []
    if _missing:
        st.caption("Dhan returned no live LTP after retries for:")
        st.dataframe(pd.DataFrame(_missing), use_container_width=True, hide_index=True)

# 2 STRATEGY REFERENCE - COLLAPSIBLE
st.divider()
with st.expander("🎯 S1 — Strategy Rules",expanded=False):
    rules=[
        ["S1","BUY","Existing window","Open between PDL/PDH → cross PDH","PDL","1.25R","SL / Target / existing square-off"],
        ["S1","SELL","Existing window","Open between PDL/PDH → cross PDL","PDH","1.25R","SL / Target / existing square-off"],
    ]
    st.dataframe(pd.DataFrame(rules,columns=["Strategy","Side","Entry Window","Exact Entry Condition","SL","Target","Exit"]),use_container_width=True,hide_index=True)

# S1 LIVE ENTRY DIAGNOSTICS
diag = market.get("entry_diagnostics", {})
with st.expander("🔎 S1 Entry Diagnostics", expanded=False):
    st.caption("Exact live gates for the current scan cycle; this does not change S1 logic.")
    d1 = st.columns(4)
    d1[0].metric("Market Mode", str(diag.get("mode", "UNKNOWN")))
    d1[1].metric("Open Positions", f"{diag.get("open_positions", 0)} / {diag.get("max_open_positions", 4)}")
    d1[2].metric("Candidates Checked", diag.get("candidates", 0))
    d1[3].metric("Entries This Cycle", diag.get("entries_opened", 0))
    d2 = st.columns(4)
    d2[0].metric("Sector Aligned", diag.get("sector_aligned", 0))
    d2[1].metric("Inside PDH–PDL", diag.get("inside_range", 0))
    d2[2].metric("Fresh Crossings", diag.get("cross_detected", 0))
    d2[3].metric("Sizing Rejected", diag.get("sizing_rejected", 0))
    st.caption(
        f"Entry window: {'ACTIVE' if diag.get('entry_window') else 'CLOSED'} • "
        f"Breadth: {'OK' if diag.get('breadth_ok') else 'BLOCKED'}"
    )

# 3 TODAY PERFORMANCE — SUMMARY FIRST, TRADE TABLE SEPARATE COLLAPSE
st.divider()
st.subheader("📅 Today's Performance")

# Always-visible summary first for quick mobile viewing
s = stats(today_items)

cards([
    ("Trades Taken",s["taken"]),("Open",s["open"]),("Closed",s["closed"]),
    ("Wins",s["wins"]),("Losses",s["losses"]),("Win %",f"{s['winpct']:.2f}%"),
    ("Realized P&L",f"₹{s['realized']:,.2f}"),("Live P&L",f"₹{s['live']:,.2f}"),("Total P&L",f"₹{s['realized']+s['live']:,.2f}"),
    ("Max Capital Used",f"₹{s['maxcap']:,.2f}"),("Min Capital Used",f"₹{s['mincap']:,.2f}")
],3)

# Full trade details only when requested
with st.expander(f"📂 Show Today's Trade Details ({len(today_items)} trades)",expanded=False):
    if tdf.empty:
        st.caption("No trades taken today.")
    else:
        st.dataframe(tdf,use_container_width=True,hide_index=True)

# 4 ALL TRADES + CUMULATIVE CARDS
st.divider()
st.subheader("📂 All Trades & Cumulative Performance")
st.markdown("**Cumulative Performance**")
# Cumulative cards show only the active finalized S1 strategy history.
# All old strategy records are excluded from the active S1 performance.
finalized_trades=[t for t in trades if t.get("strategy") == "S1"]
cs = stats(finalized_trades)

# Sector A/D analysis for every trade. Uses the A/D snapshot stored at entry,
# so historical results remain reproducible after the live market changes.
_sector_rows = []
for _t in finalized_trades:
    _ad = _t.get("sector_ad")
    try:
        _ad = float(_ad)
    except (TypeError, ValueError):
        _ad = None
    _sector_rows.append({
        "Sector": _t.get("Sector","—"),
        "Sector A/D at Entry": _ad,
        "Trades": 1,
        "P&L": pnl(_t),
        "Status": _t.get("status","—"),
    })
if _sector_rows:
    _sdf = pd.DataFrame(_sector_rows)
    _summary = []
    for _sector, _g in _sdf.groupby("Sector", dropna=False):
        _valid_ad = pd.to_numeric(_g["Sector A/D at Entry"], errors="coerce").dropna()
        _closed = _g[_g["Status"] == "CLOSED"]
        _summary.append({
            "Sector": _sector,
            "Trades": len(_g),
            "Closed": len(_closed),
            "Open": int((_g["Status"] == "OPEN").sum()),
            "Avg Sector A/D at Entry": _valid_ad.mean() if not _valid_ad.empty else None,
            "Min Sector A/D at Entry": _valid_ad.min() if not _valid_ad.empty else None,
            "Max Sector A/D at Entry": _valid_ad.max() if not _valid_ad.empty else None,
            "Total P&L": _g["P&L"].sum(),
        })
    _sector_perf = pd.DataFrame(_summary).sort_values(["Total P&L","Trades"], ascending=[False,False])
else:
    _sector_perf = pd.DataFrame()

cards([
    ("Total Trades",cs["taken"]),("Closed",cs["closed"]),("Open",cs["open"]),
    ("Wins",cs["wins"]),("Losses",cs["losses"]),("Overall Win %",f"{cs['winpct']:.2f}%"),
    ("Realized P&L",f"₹{cs['realized']:,.2f}"),("Live P&L",f"₹{cs['live']:,.2f}"),("Total P&L",f"₹{cs['realized']+cs['live']:,.2f}"),
    ("Average Win",f"₹{cs['avgwin']:,.2f}"),("Average Loss",f"₹{cs['avgloss']:,.2f}"),("Profit Factor",f"{cs['profitfactor']:.2f}"),
    ("Expectancy / Closed Trade",f"₹{cs['expectancy']:,.2f}"),("Max Profit",f"₹{cs['maxprofit']:,.2f}"),("Max Loss",f"₹{cs['maxloss']:,.2f}"),
    ("Gross Profit",f"₹{cs['grossprofit']:,.2f}"),("Gross Loss",f"₹{cs['grossloss']:,.2f}")
],3)


# 5 BUY / SELL
def show_set(title,rows,icon):
    with st.expander(f"{icon} {title} ({len(rows)})",expanded=False):
        if not rows:
            st.caption("No stocks in this set."); return
        df=pd.DataFrame(rows)
        preferred=["Symbol","Company","Sector","Trend","LTP","1Y Return %","6M Return %","1M Return %","1W Return %","1D Return %","PDH","PDL"]
        cols=[x for x in preferred if x in df.columns]
        st.dataframe(df[cols] if cols else df,use_container_width=True,hide_index=True)

st.divider(); st.subheader("Stock Sets")
show_set("BUY SET",buy_rows,"🟢")
show_set("SELL SET",sell_rows,"🔴")

# 6 SECTOR A/D - COLLAPSIBLE
st.divider()
with st.expander("🏢 Sector A/D",expanded=False):
    sb=market.get("sector_breadth",{})
    if sb:
        sdf=pd.DataFrame([{
            "Sector": name,
            "Advancing": vals.get("advances",0),
            "Declining": vals.get("declines",0),
            "Unchanged": vals.get("unchanged",0),
            "Stocks Used": vals.get("valid",0),
            "A/D": vals.get("ad_ratio")
        } for name,vals in sb.items()])
        if "A/D" in sdf.columns:
            sdf=sdf.sort_values("A/D",ascending=False,na_position="last")
        st.dataframe(sdf,use_container_width=True,hide_index=True)
        mapped_sectors=len(sb)
        sectors_with_valid=sum(1 for vals in sb.values() if vals.get("valid",0)>0)
        total_used=sum(int(vals.get("valid",0) or 0) for vals in sb.values())
        total_adv=sum(int(vals.get("advances",0) or 0) for vals in sb.values())
        total_dec=sum(int(vals.get("declines",0) or 0) for vals in sb.values())
        total_unch=sum(int(vals.get("unchanged",0) or 0) for vals in sb.values())
        st.caption(
            f"Sector Breadth Coverage • Total Sectors: {mapped_sectors} • "
            f"Sectors with Valid Data: {sectors_with_valid} • "
            f"Advancing: {total_adv} • Declining: {total_dec} • "
            f"Unchanged: {total_unch} • Stocks Used for Sector A/D: {total_used}"
        )
    else:
        st.caption("Sector A/D data will appear with live valid quotes.")

# 7 FULL NIFTY 500 LIVE DATA - COLLAPSIBLE
# trend_check owns the one shared 15-second quote scan; read its returned snapshot.
dashboard_universe_rows = market.get("breadth_universe_rows")
if not dashboard_universe_rows:
    dashboard_universe_rows = []
dashboard_live_quotes = market.get("live_quotes", {})
dashboard_resolved_pdc = market.get("resolved_pdc", {})
st.divider()
with st.expander(f"📋 Full NIFTY 500 Live Data ({len(dashboard_universe_rows)} mapped)",expanded=False):
    all_stock_rows = []
    for row in dashboard_universe_rows:
        try:
            sid = int(row.get("SecurityId"))
        except (TypeError, ValueError):
            continue
        # The live table must use the SAME fresh quote snapshot used by A/D.
        # Rows in scan_state are static and do not themselves contain refreshed LTP.
        q = dashboard_live_quotes.get(sid, {})
        live_ltp = q.get("ltp")
        row_pdc = dashboard_resolved_pdc.get(sid)
        if row_pdc is None:
            row_pdc = q.get("previous_close")
        if row_pdc is None:
            row_pdc = row.get("PDC", row.get("pdc"))
        try:
            live_ltp = float(live_ltp) if live_ltp not in (None, "") else None
        except (TypeError, ValueError):
            live_ltp = None
        try:
            row_pdc = float(row_pdc) if row_pdc not in (None, "") else None
        except (TypeError, ValueError):
            row_pdc = None
        pdh = row.get("PDH", row.get("pdh"))
        pdl = row.get("PDL", row.get("pdl"))
        # Final fallback for the stock sets/classified state. This fills any
        # symbol whose NSE range source was temporarily unavailable.
        if pdh in (None, "") or pdl in (None, ""):
            _fallback = None
            for _src in (buy_rows + sell_rows + state.get("classified", [])):
                if str(_src.get("Symbol", "")).upper().strip() == str(row.get("Symbol", "")).upper().strip():
                    _fallback = _src
                    break
            if _fallback:
                pdh = pdh if pdh not in (None, "") else _fallback.get("PDH", _fallback.get("pdh"))
                pdl = pdl if pdl not in (None, "") else _fallback.get("PDL", _fallback.get("pdl"))
        if live_ltp is not None and row_pdc not in (None, 0):
            change_pct = (float(live_ltp) - float(row_pdc)) / float(row_pdc) * 100
            ad_status = "ADVANCE" if live_ltp > row_pdc else ("DECLINE" if live_ltp < row_pdc else "UNCHANGED")
        else:
            change_pct = None
            ad_status = "NO VALID LTP/PDC"
        all_stock_rows.append({
            "Symbol": row.get("Symbol", ""),
            "Company": row.get("Company", ""),
            "Sector": row.get("Sector", row.get("Industry", "")),
            "SecurityId": sid,
            "PDC": row_pdc,
            "PDH": pdh,
            "PDL": pdl,
            "LTP": live_ltp,
            "Change % vs PDC": change_pct,
            "A/D Status": ad_status,
            "Quote Status": "LIVE" if live_ltp is not None else "NO LTP"
        })
    all_stocks_df = pd.DataFrame(all_stock_rows)
    if not all_stocks_df.empty:
        st.caption(
            f"Mapped: {len(all_stocks_df)} • Live LTP: {int(all_stocks_df['LTP'].notna().sum())} • "
            f"Valid LTP + PDC: {market.get('valid_breadth_stocks',0)} • Advancing: {market.get('advances',0)} • "
            f"Declining: {market.get('declines',0)} • Unchanged: {market.get('unchanged',0)}"
        )
        st.dataframe(all_stocks_df,use_container_width=True,hide_index=True)
    else:
        st.caption("No mapped NIFTY 500 stock data available.")

# 7 LIGHTWEIGHT EOD / BACKTEST DOWNLOAD
# Keep the website fast: analysis is exported to one Excel workbook and is not
# rendered as multiple heavy tables in the Streamlit page.
st.divider()
with st.expander("📥 EOD / Full 360° Analysis Download",expanded=False):
    # Build the export dataframe here from finalized S1 history.
    adf=trade_df(finalized_trades)
    if adf.empty:
        st.caption("No trade history yet. The complete Excel analysis will build automatically as trades are recorded.")
    else:
        x=adf.copy()
        x["Entry DateTime"]=pd.to_datetime(x["Entry Time"],errors="coerce")
        x["Exit DateTime"]=pd.to_datetime(x["Exit Time"],errors="coerce")
        x["Month"]=x["Entry DateTime"].dt.strftime("%Y-%m")
        x["Weekday"]=x["Entry DateTime"].dt.day_name()
        x["Entry Hour"]=x["Entry DateTime"].dt.strftime("%H")
        x["Closed Trade"]=x["Status"].eq("CLOSED")
        x["Win"]=x["P&L"]>0
        x["Loss"]=x["P&L"]<0

        monthly=[]
        for month,g in x.dropna(subset=["Month"]).groupby("Month"):
            closed=g[g["Closed Trade"]]
            wins=int((closed["P&L"]>0).sum()); losses=int((closed["P&L"]<0).sum())
            gp=float(closed.loc[closed["P&L"]>0,"P&L"].sum()); gl=float(closed.loc[closed["P&L"]<0,"P&L"].sum())
            monthly.append({
                "Month":month,"Trades":len(g),"Closed":len(closed),"Open":int((~g["Closed Trade"]).sum()),
                "Wins":wins,"Losses":losses,"Win %":wins/len(closed)*100 if len(closed) else 0,
                "Gross Profit":gp,"Gross Loss":gl,"Profit Factor":gp/abs(gl) if gl else 0,
                "Realized P&L":float(closed["P&L"].sum()),"Live P&L":float(g.loc[~g["Closed Trade"],"P&L"].sum()),
                "Total P&L":float(g["P&L"].sum()),"Average P&L":float(g["P&L"].mean()),
                "Best Trade":float(g["P&L"].max()),"Worst Trade":float(g["P&L"].min()),
                "Max Capital":float(g["Capital Used"].max()),"Min Capital":float(g["Capital Used"].min()),
                "Avg Capital":float(g["Capital Used"].mean()),"Total Capital Turnover":float(g["Capital Used"].sum()),
                "Avg Return %":float(g["Return %"].mean()),"Total Risk Amount":float(g["Risk Amount"].sum())
            })
        monthly_df=pd.DataFrame(monthly)

        strategy=[]
        for name,g in x.groupby("Strategy"):
            closed=g[g["Closed Trade"]]
            wins=int((closed["P&L"]>0).sum()); losses=int((closed["P&L"]<0).sum())
            gp=float(closed.loc[closed["P&L"]>0,"P&L"].sum()); gl=float(closed.loc[closed["P&L"]<0,"P&L"].sum())
            strategy.append({
                "Strategy":name,"Trades":len(g),"Closed":len(closed),"Open":int((~g["Closed Trade"]).sum()),
                "Wins":wins,"Losses":losses,"Win %":wins/len(closed)*100 if len(closed) else 0,
                "Realized P&L":float(closed["P&L"].sum()),"Live P&L":float(g.loc[~g["Closed Trade"],"P&L"].sum()),
                "Total P&L":float(g["P&L"].sum()),"Avg P&L":float(g["P&L"].mean()),
                "Best":float(g["P&L"].max()),"Worst":float(g["P&L"].min()),
                "Profit Factor":gp/abs(gl) if gl else 0,"Avg Return %":float(g["Return %"].mean()),
                "Avg Capital":float(g["Capital Used"].mean()),"Total Capital":float(g["Capital Used"].sum()),
                "Avg Risk":float(g["Risk Amount"].mean())
            })
        strategy_df=pd.DataFrame(strategy).sort_values("Total P&L",ascending=False)

        stock_df=(x.groupby(["Symbol","Stock Name","Side"],dropna=False)
            .agg(Trades=("Symbol","size"),Closed=("Closed Trade","sum"),Wins=("Win","sum"),Losses=("Loss","sum"),
                 Total_PnL=("P&L","sum"),Avg_PnL=("P&L","mean"),Best_Trade=("P&L","max"),Worst_Trade=("P&L","min"),
                 Avg_Return_pct=("Return %","mean"),Avg_Capital=("Capital Used","mean"),Total_Capital=("Capital Used","sum"),
                 Avg_Risk=("Risk Amount","mean")).reset_index())
        stock_df["Win %"]=stock_df.apply(lambda r:(r["Wins"]/r["Closed"]*100) if r["Closed"] else 0,axis=1)

        exit_df=(x.groupby(["Exit Reason","Status"],dropna=False)
            .agg(Trades=("Status","size"),Total_PnL=("P&L","sum"),Average_PnL=("P&L","mean"),
                 Best=("P&L","max"),Worst=("P&L","min")).reset_index())

        side_df=(x.groupby("Side")
            .agg(Trades=("Side","size"),Total_PnL=("P&L","sum"),Average_PnL=("P&L","mean"),
                 Best=("P&L","max"),Worst=("P&L","min"),Avg_Capital=("Capital Used","mean"),
                 Avg_Return_pct=("Return %","mean")).reset_index())

        import io
        # Exact same cumulative metrics that were previously displayed on the
        # dashboard are preserved in the downloadable workbook.
        cumulative_df = pd.DataFrame([{
            "Total Trades": cs["taken"],
            "Closed": cs["closed"],
            "Open": cs["open"],
            "Wins": cs["wins"],
            "Losses": cs["losses"],
            "Overall Win %": cs["winpct"],
            "Realized P&L": cs["realized"],
            "Live P&L": cs["live"],
            "Total P&L": cs["realized"] + cs["live"],
            "Average Win": cs["avgwin"],
            "Average Loss": cs["avgloss"],
            "Profit Factor": cs["profitfactor"],
            "Expectancy / Closed Trade": cs["expectancy"],
            "Max Profit": cs["maxprofit"],
            "Max Loss": cs["maxloss"],
            "Gross Profit": cs["grossprofit"],
            "Gross Loss": cs["grossloss"],
        }])

        export_buffer=io.BytesIO()
        with pd.ExcelWriter(export_buffer,engine="openpyxl") as writer:
            x.drop(columns=["Entry DateTime","Exit DateTime"],errors="ignore").to_excel(writer,index=False,sheet_name="All Trades")
            cumulative_df.to_excel(writer,index=False,sheet_name="Cumulative Performance")
            monthly_df.to_excel(writer,index=False,sheet_name="Monthly Analysis")
            strategy_df.to_excel(writer,index=False,sheet_name="Strategy Analysis")
            stock_df.to_excel(writer,index=False,sheet_name="Stock Analysis")
            exit_df.to_excel(writer,index=False,sheet_name="Exit Analysis")
            side_df.to_excel(writer,index=False,sheet_name="Buy Sell Analysis")

        st.caption("All detailed backtest and 360° analysis stays inside one Excel file to keep this website fast and clean.")
        st.download_button(
            "📥 Download Complete EOD / 360° Analysis",
            data=export_buffer.getvalue(),
            file_name="nifty500_complete_360_analysis.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )

st.caption("Live and closed trades are not deleted by this dashboard. This update only adds mobile-friendly display, analysis and downloads; entry, exit, SL, target, timing, sector filtering and risk logic remain unchanged.")
