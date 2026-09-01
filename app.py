"""Active Streamlit dashboard and 15-second S1/S2/S3/S4 paper-trading worker.

Pre-market BUY/SELL sets and PDH/PDL are read from the bot-state branch.
When the Streamlit app is active, this app fetches fresh Dhan market data every
15 seconds and evaluates the S1/S2/S3 paper-trading strategies.
"""

import json
import time
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st
from streamlit_autorefresh import st_autorefresh

IST = ZoneInfo("Asia/Kolkata")
REPO = "sureshr89/Nifty500TrendBot"
STATE_URL = f"https://raw.githubusercontent.com/{REPO}/bot-state/scan_state.json"
STATE_URL_CACHE_BUST = f"https://raw.githubusercontent.com/{REPO}/bot-state/scan_state.json"

APP_BUILD = "ad-previous-close-v7"

st.set_page_config(page_title="NIFTY 500 Trend Bot", page_icon="📈", layout="wide")
st_autorefresh(interval=15_000, key="trend_dashboard_refresh")

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
        ids = [int(x) for x in security_ids]
        if not ids:
            return {}
        data = self.post("/marketfeed/ohlc", {exchange_segment: ids})
        rows = ((data.get("data") or {}).get(exchange_segment) or {})
        result = {}
        for sid in ids:
            row = rows.get(str(sid), rows.get(sid, {}))
            ohlc = row.get("ohlc") or {}
            ltp = row.get("last_price")
            if ltp is None:
                continue
            previous_close = row.get("previous_close")
            if previous_close is None:
                previous_close = row.get("prev_close")
            if previous_close is None:
                previous_close = ohlc.get("previous_close")
            result[int(sid)] = {
                "ltp": float(ltp),
                "open": float(ohlc.get("open", ltp)),
                "high": float(ohlc.get("high", ltp)),
                "low": float(ohlc.get("low", ltp)),
                "previous_close": float(previous_close) if previous_close not in (None, "") else None,
            }
        return result

def now_ist():
    return datetime.now(IST)

def in_entry_window(dt):
    hhmm = dt.strftime("%H:%M")
    return "09:30" <= hhmm < "13:00"

def _github_trade_state_url():
    return f"https://api.github.com/repos/{REPO}/contents/paper_trades.json"

def load_persisted_trades():
    token = st.secrets.get("GITHUB_TOKEN")
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    response = requests.get(_github_trade_state_url(), headers=headers, params={"ref": "bot-state"}, timeout=15)
    if response.status_code == 404:
        return {"trades": [], "last_ltp": {}}
    response.raise_for_status()
    payload = response.json()
    raw = base64.b64decode(payload["content"]).decode("utf-8")
    data = json.loads(raw)
    return {"trades": data.get("trades", []), "last_ltp": data.get("last_ltp", {}), "_sha": payload.get("sha")}

def persist_trades(runtime):
    token = st.secrets.get("GITHUB_TOKEN")
    if not token:
        return
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "Content-Type": "application/json"}
    current = requests.get(_github_trade_state_url(), headers=headers, params={"ref": "bot-state"}, timeout=15)
    sha = None
    if current.status_code == 200:
        sha = current.json().get("sha")
    elif current.status_code != 404:
        current.raise_for_status()
    data = {"trades": runtime.get("trades", []), "last_ltp": runtime.get("last_ltp", {})}
    body = {
        "message": "Persist paper trade state",
        "content": base64.b64encode(json.dumps(data, separators=(",", ":")).encode("utf-8")).decode("ascii"),
        "branch": "bot-state",
    }
    if sha:
        body["sha"] = sha
    response = requests.put(_github_trade_state_url(), headers=headers, json=body, timeout=15)
    response.raise_for_status()

def trend_check(state):
    client = DhanLiveClient()
    market = dict(state.get("market", {}))
    buy_rows = state.get("buy_set", [])
sell_rows = state.get("sell_set", [])
mode = market.get("mode", "NEUTRAL")

# --------------------------- Dashboard helpers (display only) ---------------------------
def stock_name(trade):
    return trade.get("Company") or trade.get("Stock Name") or trade.get("Name") or trade.get("Symbol") or "—"

def trade_pnl(trade, live_quotes=None):
    qty = float(trade.get("quantity", 0) or 0)
    entry = float(trade.get("entry_price", 0) or 0)
    if trade.get("status") == "CLOSED":
        px = trade.get("exit_price")
    else:
        px = None
        try:
            q = (live_quotes or {}).get(int(trade.get("SecurityId")))
            px = q.get("ltp") if q else None
        except (TypeError, ValueError):
            pass
    if px is None:
        return 0.0
    px = float(px)
    return (px - entry) * qty if trade.get("side") == "BUY" else (entry - px) * qty

def trade_capital(trade):
    return float(trade.get("entry_price", 0) or 0) * float(trade.get("quantity", 0) or 0)

def trade_rows(source, live_quotes=None):
    rows = []
    for t in source:
        pnl = trade_pnl(t, live_quotes)
        rows.append({
            "Date": str(t.get("entry_time", ""))[:10],
            "Strategy": t.get("strategy", "—"),
            "Side": t.get("side", "—"),
            "Symbol": t.get("Symbol", "—"),
            "Stock Name": stock_name(t),
            "Entry Time": t.get("entry_time", "—"),
            "Entry": t.get("entry_price"),
            "Qty": t.get("quantity"),
            "Capital Used": trade_capital(t),
            "SL": t.get("SL"),
            "Target": t.get("target"),
            "Status": t.get("status", "—"),
            "Exit Time": t.get("exit_time", "—"),
            "Exit Price": t.get("exit_price"),
            "Exit Reason": t.get("exit_reason", "OPEN" if t.get("status") == "OPEN" else "—"),
            "P&L": pnl,
        })
    return rows

def summary(source, live_quotes=None):
    open_t = [t for t in source if t.get("status") == "OPEN"]
    closed = [t for t in source if t.get("status") == "CLOSED"]
    pnls = [trade_pnl(t, live_quotes) for t in closed]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    realized = sum(pnls)
    live = sum(trade_pnl(t, live_quotes) for t in open_t)
    capitals = [trade_capital(t) for t in source if trade_capital(t) > 0]
    return {
        "taken": len(source), "open": len(open_t), "closed": len(closed),
        "wins": wins, "losses": losses,
        "win_pct": (wins / len(closed) * 100) if closed else 0.0,
        "realized": realized, "live": live, "total": realized + live,
        "max_capital": max(capitals) if capitals else 0.0,
        "min_capital": min(capitals) if capitals else 0.0,
        "max_profit": max(pnls) if pnls else 0.0,
        "max_loss": min(pnls) if pnls else 0.0,
        "avg_win": (sum(p for p in pnls if p > 0) / wins) if wins else 0.0,
        "avg_loss": (sum(p for p in pnls if p < 0) / losses) if losses else 0.0,
    }

# 1. LIVE MARKET
st.subheader("📊 Live Market")
live_frame = pd.DataFrame([{
    "Worker": live_status,
    "NIFTY 500 LTP": market.get("ltp", "—"),
    "NIFTY % vs PDC": f"{float(market.get('day_pct', 0) or 0):.2f}%",
    "NIFTY 500 A/D": market.get("ad_ratio", "—"),
    "Bias": mode,
}])
st.dataframe(live_frame, use_container_width=True, hide_index=True)
st.caption(f"PDC: {market.get('pdc', '—')} • Breadth: {market.get('valid_breadth_stocks', 0)} • Adv: {market.get('advances', 0)} • Dec: {market.get('declines', 0)} • Unch: {market.get('unchanged', 0)} • Last live check: {now_ist():%H:%M:%S IST}")

# 2. STRATEGY REFERENCE
st.divider()
st.subheader("🎯 S1 / S2 / S3 / S4 — Entry / SL / Target / Exit")
strategy_rows = [
    ["S1","BUY","09:30–13:00","Open > PDH; Low ≤ PDH×0.9985; previous LTP < PDH; current LTP ≥ PDH","(PDH+PDL)/2","1.25R","SL / Target / 14:55"],
    ["S1","SELL","09:30–13:00","Open < PDL; High ≥ PDL×1.0015; previous LTP > PDL; current LTP ≤ PDL","(PDH+PDL)/2","1.25R","SL / Target / 14:55"],
    ["S2","BUY","09:30–13:00","Open between PDL & PDH; Low ≤ PDL×0.9985; reclaim PDL","PDL−(PDH−PDL)/2","1.25R","SL / Target / 14:55"],
    ["S2","SELL","09:30–13:00","Open between PDL & PDH; High ≥ PDH×1.0015; break PDH","PDH+(PDH−PDL)/2","1.25R","SL / Target / 14:55"],
    ["S3","BUY","09:30–13:00","Open < PDL; previous LTP < PDL; current LTP ≥ PDL","Today's Low","1.25R","SL / Target / 14:55"],
    ["S3","SELL","09:30–13:00","Open > PDH; previous LTP > PDH; current LTP ≤ PDH","Today's High","1.25R","SL / Target / 14:55"],
    ["S4","BUY","09:30–13:00","Open between PDL & PDH; previous LTP < PDH; current LTP ≥ PDH","(PDH+PDL)/2","1.25R","SL / Target / 14:55"],
    ["S4","SELL","09:30–13:00","Open between PDL & PDH; previous LTP > PDL; current LTP ≤ PDL","(PDH+PDL)/2","1.25R","SL / Target / 14:55"],
]
st.dataframe(pd.DataFrame(strategy_rows, columns=["Strategy","Side","Entry Window","Entry Reason","SL","Target","Exit"]), use_container_width=True, hide_index=True)

live_quotes = market.get("live_quotes", {}) if isinstance(market.get("live_quotes"), dict) else {}
today = now_ist().strftime("%Y-%m-%d")
today_trades = [t for t in trades if str(t.get("entry_time", "")).startswith(today)]

# 3. TODAY TABLE + ONLY SHORT SUMMARY
st.divider()
st.subheader("📅 Today's Performance")
today_frame = pd.DataFrame(trade_rows(today_trades, live_quotes))
if today_frame.empty:
    st.caption("No paper trades taken today.")
else:
    st.dataframe(today_frame, use_container_width=True, hide_index=True)
ts = summary(today_trades, live_quotes)
st.caption(
    f"Today: {ts['taken']} taken • {ts['open']} open • {ts['closed']} closed • "
    f"{ts['wins']} wins • {ts['losses']} losses • Win % {ts['win_pct']:.2f}% • "
    f"Realized P&L ₹{ts['realized']:,.2f} • Live P&L ₹{ts['live']:,.2f} • Total P&L ₹{ts['total']:,.2f} • "
    f"Max Capital ₹{ts['max_capital']:,.2f} • Min Capital ₹{ts['min_capital']:,.2f}"
)

# 4. ALL TRADES + CUMULATIVE TEXT
st.divider()
st.subheader("📂 All Trades")
all_frame = pd.DataFrame(trade_rows(trades, live_quotes))
if all_frame.empty:
    st.caption("No paper trades yet.")
else:
    with st.expander(f"Show complete trade history ({len(trades)})", expanded=False):
        st.dataframe(all_frame, use_container_width=True, hide_index=True)

cs = summary(trades, live_quotes)
st.markdown("**Cumulative Performance**")
st.caption(
    f"Total Trades: {cs['taken']} • Closed: {cs['closed']} • Open: {cs['open']} • "
    f"Wins: {cs['wins']} • Losses: {cs['losses']} • Overall Win %: {cs['win_pct']:.2f}% • "
    f"Realized P&L: ₹{cs['realized']:,.2f} • Live P&L: ₹{cs['live']:,.2f} • Total P&L: ₹{cs['total']:,.2f} • "
    f"Avg Win: ₹{cs['avg_win']:,.2f} • Avg Loss: ₹{cs['avg_loss']:,.2f} • "
    f"Max Profit: ₹{cs['max_profit']:,.2f} • Max Loss: ₹{cs['max_loss']:,.2f} • "
    f"Max Capital: ₹{cs['max_capital']:,.2f} • Min Capital: ₹{cs['min_capital']:,.2f}"
)

# 5. BUY / SELL SETS
def show_set(title, rows, icon):
    with st.expander(f"{icon} {title} ({len(rows)})", expanded=False):
        if not rows:
            st.caption("No stocks in this set.")
            return
        frame = pd.DataFrame(rows)
        preferred = ["Symbol", "Company", "Sector", "Trend", "LTP", "1Y Return %", "6M Return %", "1M Return %", "1W Return %", "1D Return %", "PDH", "PDL"]
        cols = [x for x in preferred if x in frame.columns]
        st.dataframe(frame[cols] if cols else frame, use_container_width=True, hide_index=True)

st.divider()
st.subheader("Stock Sets")
show_set("BUY SET", buy_rows, "🟢")
show_set("SELL SET", sell_rows, "🔴")

# 6. SECTOR A/D
sector_breadth = market.get("sector_breadth", {})
st.divider()
st.subheader("🏢 Sector A/D")
if sector_breadth:
    sector_frame = pd.DataFrame([{"Sector": s, **v} for s, v in sector_breadth.items()])
    sector_frame = sector_frame.sort_values("ad_ratio", ascending=False, na_position="last")
    st.dataframe(sector_frame, use_container_width=True, hide_index=True)
else:
    st.caption("Sector A/D will appear when live valid quotes are available.")

# 7. MONTH-WISE CUMULATIVE + DOWNLOAD
st.divider()
st.subheader("📥 Month-wise Cumulative Performance")
monthly_rows = []
if trades:
    temp = pd.DataFrame(trade_rows(trades, live_quotes))
    if not temp.empty:
        temp["Month"] = pd.to_datetime(temp["Date"], errors="coerce").dt.strftime("%Y-%m")
        for month, grp in temp.dropna(subset=["Month"]).groupby("Month"):
            closed = grp[grp["Status"] == "CLOSED"]
            wins = int((closed["P&L"] > 0).sum())
            losses = int((closed["P&L"] < 0).sum())
            capitals = grp["Capital Used"].dropna()
            monthly_rows.append({
                "Month": month,
                "Trades": len(grp),
                "Wins": wins,
                "Losses": losses,
                "Win %": (wins / len(closed) * 100) if len(closed) else 0.0,
                "Realized P&L": float(closed["P&L"].sum()) if len(closed) else 0.0,
                "Live P&L": float(grp[grp["Status"] == "OPEN"]["P&L"].sum()) if len(grp) else 0.0,
                "Total P&L": float(grp["P&L"].sum()),
                "Max Capital Used": float(capitals.max()) if len(capitals) else 0.0,
                "Min Capital Used": float(capitals.min()) if len(capitals) else 0.0,
            })
monthly_frame = pd.DataFrame(monthly_rows)
if monthly_frame.empty:
    st.caption("Month-wise data will build automatically from persistent trade history.")
else:
    st.dataframe(monthly_frame, use_container_width=True, hide_index=True)
    st.download_button(
        "📥 Download Month-wise Performance CSV",
        monthly_frame.to_csv(index=False).encode("utf-8"),
        file_name="nifty500_trend_bot_monthly_performance.csv",
        mime="text/csv",
    )

st.caption("Trade history is persisted on the bot-state branch. Dashboard changes above are display/reporting only; live strategy entry, SL, target, timing and risk logic are unchanged.")
