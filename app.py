"""Streamlit dashboard for the NIFTY 500 multi-timeframe trend scanner."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

from bot_engine import scan_nifty500

IST = ZoneInfo("Asia/Kolkata")

st.set_page_config(
    page_title="NIFTY 500 Trend Scanner",
    page_icon="📈",
    layout="wide",
)

st.title("📈 NIFTY 500 Trend Scanner")
st.caption("Dhan-only market data • Multi-timeframe stock classification")
st.caption(f"IST: {datetime.now(IST):%d-%m-%Y %H:%M:%S}")

@st.cache_data(ttl=60, show_spinner="Loading NIFTY 500 data from Dhan…")
def load_scan():
    return scan_nifty500()

try:
    result = load_scan()
except Exception as exc:
    st.error("Unable to complete the Dhan NIFTY 500 scan.")
    st.exception(exc)
    st.stop()

market = result["market"]
classified = result["classified"]
buy_set = result["buy_set"]
sell_set = result["sell_set"]

m1, m2, m3, m4 = st.columns(4)
m1.metric("NIFTY 500", f"{market['ltp']:,.2f}")
m2.metric("PDC", f"{market['pdc']:,.2f}")
m3.metric("Day %", f"{market['day_pct']:.2f}%")
m4.metric("Market Mode", market["mode"])

st.subheader("Market Filter")
if market["mode"] == "BUY":
    st.success("🟢 NIFTY 500 is above PDC — BUY side is active.")
elif market["mode"] == "SELL":
    st.error("🔴 NIFTY 500 is below PDC — SELL side is active.")
else:
    st.warning("🟡 NIFTY 500 is exactly at PDC — no side is active.")

c1, c2 = st.columns(2)
c1.metric("BUY Set", len(buy_set))
c2.metric("SELL Set", len(sell_set))

show_cols = [
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
        buy_set[show_cols].sort_values("Symbol"),
        use_container_width=True,
        hide_index=True,
    )

st.subheader("🔴 SELL SET")
if sell_set.empty:
    st.info("No stock is bearish across all four timeframes.")
else:
    st.dataframe(
        sell_set[show_cols].sort_values("Symbol"),
        use_container_width=True,
        hide_index=True,
    )

with st.expander("View all 500 stocks"):
    st.dataframe(
        classified[
            [
                "Symbol",
                "Company",
                "Sector",
                "1Y Return %",
                "6M Return %",
                "1M Return %",
                "1W Return %",
                "Trend",
            ]
        ].sort_values("Symbol"),
        use_container_width=True,
        hide_index=True,
    )

st.caption("No entry, exit, paper-trading, or order execution logic is included in this phase.")
