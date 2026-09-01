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

def _pnl(t):
    q=float(t.get("quantity",0) or 0); e=float(t.get("entry_price",0) or 0)
    px=t.get("exit_price") if t.get("status")=="CLOSED" else None
    if px is None:
        try:
            x=market.get("live_quotes",{}).get(int(t.get("SecurityId")))
            px=x.get("ltp") if x else e
        except Exception: px=e
    px=float(px or e)
    return (px-e)*q if t.get("side")=="BUY" else (e-px)*q

def _cap(t): return float(t.get("entry_price",0) or 0)*float(t.get("quantity",0) or 0)

def _rows(items):
    return pd.DataFrame([{
        "Date":str(t.get("entry_time",""))[:10],"Strategy":t.get("strategy","—"),"Side":t.get("side","—"),
        "Symbol":t.get("Symbol","—"),"Stock Name":t.get("Company") or t.get("Stock Name") or t.get("Symbol","—"),
        "Entry Time":t.get("entry_time","—"),"Entry":t.get("entry_price"),"Qty":t.get("quantity"),
        "Capital Used":_cap(t),"SL":t.get("SL"),"Target":t.get("target"),"Status":t.get("status","—"),
        "Exit Time":t.get("exit_time","—"),"Exit Price":t.get("exit_price"),
        "Exit Reason":t.get("exit_reason","OPEN" if t.get("status")=="OPEN" else "—"),"P&L":_pnl(t)
    } for t in items])

def _summary(items):
    closed=[t for t in items if t.get("status")=="CLOSED"]; opened=[t for t in items if t.get("status")=="OPEN"]
    pn=[_pnl(t) for t in closed]; wins=sum(x>0 for x in pn); losses=sum(x<0 for x in pn)
    caps=[_cap(t) for t in items if _cap(t)>0]
    return dict(taken=len(items),open=len(opened),closed=len(closed),wins=wins,losses=losses,
        win=(wins/len(closed)*100 if closed else 0),real=sum(pn),live=sum(_pnl(t) for t in opened),
        maxcap=max(caps) if caps else 0,mincap=min(caps) if caps else 0,
        avgwin=(sum(x for x in pn if x>0)/wins if wins else 0),avgloss=(sum(x for x in pn if x<0)/losses if losses else 0),
        maxprofit=max(pn) if pn else 0,maxloss=min(pn) if pn else 0)

# 1 LIVE MARKET
st.subheader("📊 Live Market")
st.dataframe(pd.DataFrame([{"Worker":live_status,"NIFTY 500 LTP":market.get("ltp","—"),
    "NIFTY % vs PDC":f"{float(market.get('day_pct',0) or 0):.2f}%","NIFTY 500 A/D":market.get("ad_ratio","—"),"Bias":mode}]),
    use_container_width=True,hide_index=True)

# 2 STRATEGY REFERENCE
st.divider(); st.subheader("🎯 S1 / S2 / S3 / S4")
rules=[
["S1","BUY","09:30–13:00","Open > PDH; pullback 0.15%; reclaim PDH","(PDH+PDL)/2","1.25R","SL / Target / 14:55"],
["S1","SELL","09:30–13:00","Open < PDL; pullback 0.15%; break PDL","(PDH+PDL)/2","1.25R","SL / Target / 14:55"],
["S2","BUY","09:30–13:00","Open inside range; below PDL 0.15%; reclaim PDL","PDL−(PDH−PDL)/2","1.25R","SL / Target / 14:55"],
["S2","SELL","09:30–13:00","Open inside range; above PDH 0.15%; break PDH","PDH+(PDH−PDL)/2","1.25R","SL / Target / 14:55"],
["S3","BUY","09:30–13:00","Open below PDL; reclaim PDL","Today's Low","1.25R","SL / Target / 14:55"],
["S3","SELL","09:30–13:00","Open above PDH; break below PDH","Today's High","1.25R","SL / Target / 14:55"],
["S4","BUY","09:30–13:00","Open inside range; cross above PDH","(PDH+PDL)/2","1.25R","SL / Target / 14:55"],
["S4","SELL","09:30–13:00","Open inside range; cross below PDL","(PDH+PDL)/2","1.25R","SL / Target / 14:55"]]
st.dataframe(pd.DataFrame(rules,columns=["Strategy","Side","Entry Window","Entry Reason","SL","Target","Exit"]),
    use_container_width=True,hide_index=True)

# 3 TODAY
today=now_ist().strftime("%Y-%m-%d")
today_trades=[t for t in trades if str(t.get("entry_time","")).startswith(today)]
st.divider(); st.subheader("📅 Today's Performance")
tf=_rows(today_trades)
if tf.empty: st.caption("No paper trades taken today.")
else: st.dataframe(tf,use_container_width=True,hide_index=True)
s=_summary(today_trades)
st.caption(f"Today: {s['taken']} taken • {s['open']} open • {s['closed']} closed • {s['wins']} wins • {s['losses']} losses • Win % {s['win']:.2f}% • Realized P&L ₹{s['real']:,.2f} • Live P&L ₹{s['live']:,.2f} • Total P&L ₹{s['real']+s['live']:,.2f} • Max Capital ₹{s['maxcap']:,.2f} • Min Capital ₹{s['mincap']:,.2f}")

# 4 ALL TRADES + CUMULATIVE
st.divider(); st.subheader("📂 All Trades")
af=_rows(trades)
with st.expander(f"Show complete trade history ({len(trades)})",expanded=False):
    if af.empty: st.caption("No paper trades yet.")
    else: st.dataframe(af,use_container_width=True,hide_index=True)
c=_summary(trades)
st.markdown("**Cumulative Performance**")
st.caption(f"Total Trades: {c['taken']} • Closed: {c['closed']} • Open: {c['open']} • Wins: {c['wins']} • Losses: {c['losses']} • Overall Win %: {c['win']:.2f}% • Total Realized P&L ₹{c['real']:,.2f} • Live P&L ₹{c['live']:,.2f} • Total P&L ₹{c['real']+c['live']:,.2f} • Avg Win ₹{c['avgwin']:,.2f} • Avg Loss ₹{c['avgloss']:,.2f} • Max Profit ₹{c['maxprofit']:,.2f} • Max Loss ₹{c['maxloss']:,.2f} • Max Capital ₹{c['maxcap']:,.2f} • Min Capital ₹{c['mincap']:,.2f}")

def show_set(title, rows, icon):
    with st.expander(f"{icon} {title} ({len(rows)})",expanded=False):
        if not rows: st.caption("No stocks in this set."); return
        df=pd.DataFrame(rows)
        preferred=["Symbol","Company","Sector","Trend","LTP","1Y Return %","6M Return %","1M Return %","1W Return %","1D Return %","PDH","PDL"]
        cols=[x for x in preferred if x in df.columns]
        st.dataframe(df[cols] if cols else df,use_container_width=True,hide_index=True)

# 5 SETS
st.divider(); st.subheader("Stock Sets")
show_set("BUY SET",buy_rows,"🟢"); show_set("SELL SET",sell_rows,"🔴")

# 6 SECTOR A/D
st.divider(); st.subheader("🏢 Sector A/D")
sb=market.get("sector_breadth",{})
if sb:
    sdf=pd.DataFrame([{"Sector":k,**v} for k,v in sb.items()])
    if "ad_ratio" in sdf.columns: sdf=sdf.sort_values("ad_ratio",ascending=False)
    st.dataframe(sdf,use_container_width=True,hide_index=True)
else: st.caption("Sector A/D will appear when live valid quotes are available.")

# 7 MONTHLY
st.divider(); st.subheader("📥 Month-wise Cumulative Performance")
if not af.empty:
    x=af.copy(); x["Month"]=pd.to_datetime(x["Date"],errors="coerce").dt.strftime("%Y-%m")
    out=[]
    for m,g in x.dropna(subset=["Month"]).groupby("Month"):
        cl=g[g["Status"]=="CLOSED"]; wins=int((cl["P&L"]>0).sum()); losses=int((cl["P&L"]<0).sum())
        out.append({"Month":m,"Trades":len(g),"Wins":wins,"Losses":losses,"Win %":wins/len(cl)*100 if len(cl) else 0,
            "Realized P&L":float(cl["P&L"].sum()),"Live P&L":float(g[g["Status"]=="OPEN"]["P&L"].sum()),
            "Total P&L":float(g["P&L"].sum()),"Max Capital Used":float(g["Capital Used"].max()),"Min Capital Used":float(g["Capital Used"].min())})
    mf=pd.DataFrame(out); st.dataframe(mf,use_container_width=True,hide_index=True)
    st.download_button("📥 Download Month-wise Performance CSV",mf.to_csv(index=False).encode(),"nifty500_trend_bot_monthly_performance.csv","text/csv")
else: st.caption("Month-wise data will build automatically from persistent trade history.")

st.caption("Trade history is persistent; dashboard changes above are display-only and do not change live strategy entry, SL, target, timing or risk logic.")
