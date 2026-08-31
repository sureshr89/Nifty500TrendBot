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

# --------------------------- Professional mobile dashboard ---------------------------
st.markdown("""
<style>
.block-container {max-width: 1200px; padding-top: 1rem; padding-bottom: 2rem;}
[data-testid="stMetric"] {background: rgba(255,255,255,.04); border: 1px solid rgba(128,128,128,.18); border-radius: 14px; padding: 10px 12px;}
[data-testid="stMetricLabel"] {font-size: .78rem;}
[data-testid="stMetricValue"] {font-size: 1.15rem;}
div[data-testid="stExpander"] {border-radius: 12px;}
@media (max-width: 640px) {
  .block-container {padding-left: .65rem; padding-right: .65rem; padding-top: .5rem;}
  h1 {font-size: 1.55rem !important;}
  [data-testid="stMetric"] {padding: 8px;}
  [data-testid="stMetricValue"] {font-size: 1rem;}
}
</style>
""", unsafe_allow_html=True)

st.title("📈 NIFTY 500 Trend Bot")
st.caption("Live Dhan monitoring • S1 paper trading")
st.caption(f"IST • {now_ist():%d %b %Y, %H:%M:%S}  |  🔄 Auto refresh: 15 sec")

try:
    state = load_premarket_state()
    market, trades = s1_check(state)
    live_status = "🟢 ACTIVE"
except Exception as exc:
    state = {}
    market = {}
    trades = st.session_state.get("s1_runtime", {}).get("trades", [])
    live_status = "🔴 ERROR"
    st.error(str(exc))

buy_rows = state.get("buy_set", [])
sell_rows = state.get("sell_set", [])
mode = market.get("mode", "NEUTRAL")

st.subheader("Live Market")
top1, top2 = st.columns(2)
top1.metric("Worker", live_status)
top2.metric("Market Mode", mode)

m1, m2, m3 = st.columns(3)
m1.metric("NIFTY 500", market.get("ltp", "—"))
m2.metric("Day %", f"{float(market.get('day_pct', 0)):.2f}%")
m3.metric("A/D Ratio", market.get("ad_ratio", "—"))

st.caption(f"PDC: {market.get('pdc', '—')}  •  Last live check: {now_ist():%H:%M:%S IST}")

st.divider()
st.subheader("🎯 S1 Strategy Status")
s1a, s1b, s1c = st.columns(3)
s1a.metric("Entry Window", "09:30–13:00")
s1b.metric("Pullback", "0.15%")
s1c.metric("Square-off", "14:55")

st.caption("BUY: Open > PDH → pullback 0.15% below PDH → reclaim PDH")
st.caption("SELL: Open < PDL → retrace 0.15% above PDL → break below PDL")

open_trade = next((t for t in trades if t.get("status") == "OPEN"), None)
if open_trade:
    st.success(f"OPEN • {open_trade.get('side')} • {open_trade.get('Symbol')}")
    a,b,c1,d = st.columns(4)
    a.metric("Entry", f"₹{open_trade.get('entry_price', 0):.2f}")
    b.metric("SL", f"₹{open_trade.get('SL', 0):.2f}")
    c1.metric("Target", f"₹{open_trade.get('target', 0):.2f}")
    d.metric("Status", "OPEN")
else:
    st.info("No open S1 paper position")

st.divider()
st.subheader("📋 S1 Trade History")
if trades:
    trade_frame = pd.DataFrame(trades)
    mobile_cols = [c for c in ["Symbol","side","status","entry_price","SL","target","exit_price","exit_reason","entry_time"] if c in trade_frame.columns]
    st.dataframe(trade_frame[mobile_cols], use_container_width=True, hide_index=True)
else:
    st.caption("No paper trades yet.")

def show_set(title, rows, icon):
    with st.expander(f"{icon} {title} ({len(rows)})", expanded=False):
        if not rows:
            st.caption("No stocks in this set.")
            return
        frame = pd.DataFrame(rows)
        preferred = ["Symbol", "Company", "Sector", "PDH", "PDL", "SecurityId"]
        cols = [x for x in preferred if x in frame.columns]
        st.dataframe(frame[cols] if cols else frame, use_container_width=True, hide_index=True)

st.divider()
st.subheader("Stock Sets")
show_set("BUY SET", buy_rows, "🟢")
show_set("SELL SET", sell_rows, "🔴")

st.caption("Live worker runs while this Streamlit session remains active. Dhan credentials are kept in Streamlit Secrets.")

