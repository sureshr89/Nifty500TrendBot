"""Dhan-only NIFTY 500 trend dashboard.

Phase 1 only: classify the NIFTY 500 into BUY/SELL sets using four
positive/negative return timeframes and display the current NIFTY 500
market mode relative to PDC. No trading or order logic is included.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from bot_engine import scan_nifty500

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(
    page_title="NIFTY 500 Trend Bot",
    page_icon="📈",
    layout="wide",
)

st.title("📈 NIFTY 500 Trend Bot")
st.caption("Dhan-only data • 1Y / 6M / 1M / 1W trend classification")
st.caption(f"IST: {datetime.now(IST):%d-%m-%Y %H:%M:%S}")

@st.cache_data(ttl=300, show_spinner="Loading NIFTY 500 data from Dhan…")
def load_scan():
    return scan_nifty500()

try:
    result = load_scan()
except Exception as exc:
    st.error("Dhan scan failed.")
    st.exception(exc)
    st.stop()

market = result["market"]
frame = result["classified"]
buy_set = result["buy_set"]
sell_set = result["sell_set"]

m1, m2, m3, m4 = st.columns(4)
m1.metric("NIFTY 500 LTP", f"{market['ltp']:,.2f}")
m2.metric("NIFTY 500 PDC", f"{market['pdc']:,.2f}")
m3.metric("Day %", f"{market['day_pct']:.2f}%")
m4.metric("Market Mode", market["mode"])

if market["mode"] == "BUY":
    st.success("🟢 NIFTY 500 is above PDC — BUY side is active.")
elif market["mode"] == "SELL":
    st.error("🔴 NIFTY 500 is below PDC — SELL side is active.")
else:
    st.warning("🟡 NIFTY 500 is at PDC — neutral.")

c1, c2 = st.columns(2)
c1.metric("BUY SET", len(buy_set))
c2.metric("SELL SET", len(sell_set))

columns = [
    "Symbol",
    "Company",
    "Sector",
    "1Y Return %",
    "6M Return %",
    "1M Return %",
    "1W Return %",
    "Trend",
]

st.subheader("🟢 BUY SET")
if buy_set.empty:
    st.info("No stock is bullish across all four timeframes.")
else:
    st.dataframe(
        buy_set[columns].sort_values("Symbol"),
        use_container_width=True,
        hide_index=True,
    )

st.subheader("🔴 SELL SET")
if sell_set.empty:
    st.info("No stock is bearish across all four timeframes.")
else:
    st.dataframe(
        sell_set[columns].sort_values("Symbol"),
        use_container_width=True,
        hide_index=True,
    )

st.subheader("All NIFTY 500")
st.dataframe(
    frame[columns].sort_values("Symbol"),
    use_container_width=True,
    hide_index=True,
)

st.caption("Phase 1 only — no entry, exit, paper-trading, or order execution logic is included.")
