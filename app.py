"""Display-only Streamlit dashboard.

GitHub Actions performs all Dhan API work. This app never imports the engine
and never calls Dhan. It only reads the latest persisted state.
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
def load_state():
    response = requests.get(STATE_URL, timeout=15)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Invalid state format")
    return data

st.title("📈 NIFTY 500 Trend Bot")
st.caption("GitHub Actions → Dhan → 15-second LTP worker → persisted state → Dashboard")
st.caption(f"IST: {datetime.now(IST):%d-%m-%Y %H:%M:%S}")
st.caption("🔄 Dashboard refresh every 15 seconds")

try:
    state = load_state()
except Exception as exc:
    st.warning("No GitHub worker state is available yet. Run the pre-market scan first.")
    st.caption(str(exc))
    st.stop()

market = state.get("market", {})
health = state.get("health", {})
buy_rows = state.get("buy_set", [])
sell_rows = state.get("sell_set", [])

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Worker Status", health.get("worker_status", "unknown"))
c2.metric("Last Scan", health.get("last_scan_ist", "Not available"))
c3.metric("NIFTY 500 LTP", market.get("ltp", "—"))
c4.metric("A/D Ratio", market.get("ad_ratio", "—"))
c5.metric("Market Mode", market.get("mode", "NEUTRAL"))

st.caption(
    f"PDC: {market.get('pdc', '—')} | Day %: {market.get('day_pct', '—')} | "
    f"Advances: {market.get('advances', '—')} | Declines: {market.get('declines', '—')}"
)

def show_set(title, rows):
    st.subheader(title)
    if not rows:
        st.info("No stocks in this set.")
        return
    frame = pd.DataFrame(rows)
    preferred = ["Symbol", "Company", "Sector", "SecurityId", "1Y Return %", "6M Return %", "1M Return %", "1W Return %", "Trend"]
    columns = [c for c in preferred if c in frame.columns]
    st.dataframe(frame[columns] if columns else frame, use_container_width=True, hide_index=True)

show_set(f"🟢 BUY SET ({len(buy_rows)})", buy_rows)
show_set(f"🔴 SELL SET ({len(sell_rows)})", sell_rows)

st.header("🎯 S1 Strategy")
st.caption("Entry: 09:30–13:00 IST | Live check: 15 seconds | Pullback/retracement: 0.15% | Auto square-off: 14:55 IST")
s1 = state.get("s1", {})
trades = s1.get("trades", [])
if trades:
    st.dataframe(pd.DataFrame(trades), use_container_width=True, hide_index=True)
else:
    st.info("No S1 trade yet.")

if health.get("last_error"):
    st.error(health["last_error"])

st.caption("Dashboard only. No Dhan credentials and no Dhan API calls are required here.")
