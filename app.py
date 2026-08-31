"""Active Streamlit dashboard and 15-second S1 paper-trading worker.

Pre-market BUY/SELL sets and PDH/PDL are read from the bot-state branch.
When the Streamlit app is active, this app fetches fresh Dhan market data every
15 seconds and evaluates the S1 paper-trading strategy.
"""

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

IST = ZoneInfo("Asia/Kolkata")
REPO = "sureshr89/Nifty500TrendBot"
STATE_URL = f"https://raw.githubusercontent.com/{REPO}/bot-state/scan_state.json"

st.set_page_config(page_title="NIFTY 500 Trend Bot", page_icon="📈", layout="wide")
st_autorefresh(interval=15_000, key="trend_dashboard_refresh")

@st.cache_data(ttl=10, show_spinner=False)
def load_premarket_state():
    response = requests.get(STATE_URL, timeout=15)
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
        response = requests.post(
            "https://api.dhan.co/v2" + path,
            headers=self.headers,
            json=payload,
            timeout=15,
        )
        response.raise_for_status()
        return response.json()

    def quotes(self, security_ids):
        ids = [str(int(x)) for x in security_ids]
        if not ids:
            return {}
        data = self.post("/marketfeed/ohlc", {"NSE_EQ": ids})
        rows = ((data.get("data") or {}).get("NSE_EQ") or {})
        result = {}
        for sid in ids:
            row = rows.get(sid, {})
            ohlc = row.get("ohlc") or {}
            ltp = row.get("last_price")
            if ltp is None:
                continue
            result[int(sid)] = {
                "ltp": float(ltp),
                "open": float(ohlc.get("open", ltp)),
                "high": float(ohlc.get("high", ltp)),
                "low": float(ohlc.get("low", ltp)),
            }
        return result

def now_ist():
    return datetime.now(IST)

def in_entry_window(dt):
    hhmm = dt.strftime("%H:%M")
    return "09:30" <= hhmm < "13:00"

def s1_check(state):
    client = DhanLiveClient()
    market = dict(state.get("market", {}))
    buy_rows = state.get("buy_set", [])
    sell_rows = state.get("sell_set", [])
    ad_ratio = float(market.get("ad_ratio", 1.0))
    ltp = market.get("ltp")
    pdc = market.get("pdc")

    # NIFTY live refresh uses the same saved market security configuration.
    nifty_sid = market.get("security_id") or market.get("SecurityId")
    if nifty_sid:
        nq = client.quotes([nifty_sid]).get(int(nifty_sid))
        if nq:
            ltp = nq["ltp"]

    if ltp is not None and pdc:
        day_pct = (float(ltp) - float(pdc)) / float(pdc) * 100
    else:
        day_pct = market.get("day_pct", 0)

    mode = "BUY" if day_pct > 0 and ad_ratio > 1 else ("SELL" if day_pct < 0 and ad_ratio < 1 else "NEUTRAL")
    market.update({"ltp": ltp, "day_pct": day_pct, "ad_ratio": ad_ratio, "mode": mode})

    if "s1_runtime" not in st.session_state:
        st.session_state.s1_runtime = {"trades": [], "last_ltp": {}}
    runtime = st.session_state.s1_runtime
    trades = runtime["trades"]
    open_trade = next((t for t in trades if t["status"] == "OPEN"), None)
    dt = now_ist()

    # 14:55 mandatory square-off.
    if open_trade and dt.strftime("%H:%M") >= "14:55":
        q = client.quotes([open_trade["SecurityId"]]).get(int(open_trade["SecurityId"]))
        if q:
            open_trade.update({"status": "CLOSED", "exit_price": q["ltp"], "exit_reason": "AUTO_SQUARE_OFF", "exit_time": dt.strftime("%Y-%m-%d %H:%M:%S IST")})
            open_trade = None

    # SL / target monitoring.
    if open_trade:
        q = client.quotes([open_trade["SecurityId"]]).get(int(open_trade["SecurityId"]))
        if q:
            px = q["ltp"]
            if open_trade["side"] == "BUY":
                reason = "STOP_LOSS" if px <= open_trade["SL"] else ("TARGET" if px >= open_trade["target"] else None)
            else:
                reason = "STOP_LOSS" if px >= open_trade["SL"] else ("TARGET" if px <= open_trade["target"] else None)
            if reason:
                open_trade.update({"status": "CLOSED", "exit_price": px, "exit_reason": reason, "exit_time": dt.strftime("%Y-%m-%d %H:%M:%S IST")})
                open_trade = None

    # New S1 entry: one position at a time.
    if not open_trade and in_entry_window(dt) and mode in ("BUY", "SELL"):
        candidates = buy_rows if mode == "BUY" else sell_rows
        ids = [x["SecurityId"] for x in candidates]
        quotes = client.quotes(ids)
        for stock in candidates:
            sid = int(stock["SecurityId"])
            q = quotes.get(sid)
            if not q:
                continue
            pdh, pdl = float(stock.get("PDH", 0)), float(stock.get("PDL", 0))
            if pdh <= 0 or pdl <= 0:
                continue
            prev_ltp = runtime["last_ltp"].get(sid)

            if mode == "BUY":
                pulled_back = q["open"] > pdh and q["low"] <= pdh * 0.9985
                crossed = (prev_ltp is None or prev_ltp < pdh) and q["ltp"] >= pdh
                if pulled_back and crossed:
                    sl = (pdh + pdl) / 2
                    risk = q["ltp"] - sl
                    if risk > 0:
                        trades.append({"strategy":"S1","side":"BUY","status":"OPEN","Symbol":stock["Symbol"],"SecurityId":sid,"entry_price":q["ltp"],"PDH":pdh,"PDL":pdl,"SL":sl,"target":q["ltp"] + 2*risk,"entry_time":dt.strftime("%Y-%m-%d %H:%M:%S IST")})
                        break
            else:
                retraced = q["open"] < pdl and q["high"] >= pdl * 1.0015
                crossed = (prev_ltp is None or prev_ltp > pdl) and q["ltp"] <= pdl
                if retraced and crossed:
                    sl = (pdh + pdl) / 2
                    risk = sl - q["ltp"]
                    if risk > 0:
                        trades.append({"strategy":"S1","side":"SELL","status":"OPEN","Symbol":stock["Symbol"],"SecurityId":sid,"entry_price":q["ltp"],"PDH":pdh,"PDL":pdl,"SL":sl,"target":q["ltp"] - 2*risk,"entry_time":dt.strftime("%Y-%m-%d %H:%M:%S IST")})
                        break

            runtime["last_ltp"][sid] = q["ltp"]

    return market, runtime["trades"]

st.title("📈 NIFTY 500 Trend Bot")
st.caption("Active Streamlit worker → Dhan live LTP → S1 paper trade")
st.caption(f"IST: {now_ist():%d-%m-%Y %H:%M:%S}")
st.caption("🔄 Live Dhan check every 15 seconds while this Streamlit app session is active")

try:
    state = load_premarket_state()
    market, trades = s1_check(state)
    live_status = "ACTIVE"
except Exception as exc:
    state = {}
    market = {}
    trades = st.session_state.get("s1_runtime", {}).get("trades", [])
    live_status = "ERROR"
    st.error(str(exc))

health = state.get("health", {})
buy_rows = state.get("buy_set", [])
sell_rows = state.get("sell_set", [])

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Live Worker", live_status)
c2.metric("Last Check", now_ist().strftime("%H:%M:%S IST"))
c3.metric("NIFTY 500 LTP", market.get("ltp", "—"))
c4.metric("A/D Ratio", market.get("ad_ratio", "—"))
c5.metric("Market Mode", market.get("mode", "NEUTRAL"))

st.caption(f"PDC: {market.get('pdc', '—')} | Day %: {market.get('day_pct', '—')}")

def show_set(title, rows):
    st.subheader(title)
    if not rows:
        st.info("No stocks in this set.")
        return
    frame = pd.DataFrame(rows)
    preferred = ["Symbol", "Company", "Sector", "SecurityId", "PDH", "PDL", "Trend"]
    columns = [c for c in preferred if c in frame.columns]
    st.dataframe(frame[columns] if columns else frame, use_container_width=True, hide_index=True)

show_set(f"🟢 BUY SET ({len(buy_rows)})", buy_rows)
show_set(f"🔴 SELL SET ({len(sell_rows)})", sell_rows)

st.header("🎯 S1 Active Paper Trading")
st.caption("BUY: Open > PDH → Low ≤ PDH − 0.15% → LTP crosses ≥ PDH")
st.caption("SELL: Open < PDL → High ≥ PDL + 0.15% → LTP crosses ≤ PDL")
st.caption("Entry 09:30–13:00 IST | SL/Target monitored | Auto square-off 14:55 IST")

if trades:
    st.dataframe(pd.DataFrame(trades), use_container_width=True, hide_index=True)
else:
    st.info("No S1 paper trade yet.")

st.caption("Keep this Streamlit app session active for continuous 15-second live checking.")
