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
    # Build live breadth universe and fetch NSE equities once per refresh.
    universe_rows = state.get("breadth_universe", []) or state.get("classified", []) or buy_rows + sell_rows

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

    live_quotes = client.quotes(sorted(set(universe_ids)), exchange_segment="NSE_EQ")

    # Populate BUY/SELL table LTP from the same quote batch.
    for stock in buy_rows + sell_rows:
        try:
            q = live_quotes.get(int(stock.get("SecurityId")))
            if q:
                stock["LTP"] = q["ltp"]
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

    valid_ids = [sid for sid, q in live_quotes.items() if sid in resolved_pdc and q.get("ltp") is not None]
    valid_count = len(valid_ids)
    advances = sum(1 for sid in valid_ids if live_quotes[sid]["ltp"] > resolved_pdc[sid])
    declines = sum(1 for sid in valid_ids if live_quotes[sid]["ltp"] < resolved_pdc[sid])
    unchanged = sum(1 for sid in valid_ids if live_quotes[sid]["ltp"] == resolved_pdc[sid])

    # Live sector-wise A/D for the same NIFTY 500 breadth universe.
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

    # Use every valid Dhan quote available. Do not block A/D behind an arbitrary
    # 480-stock threshold; the dashboard should show live breadth whenever Dhan
    # returns enough advancing/declining data to calculate it.
    breadth_valid = valid_count > 0
    if declines > 0:
        ad_ratio = advances / declines
    elif advances > 0:
        ad_ratio = float("inf")
    else:
        ad_ratio = None
    ltp = market.get("ltp")
    pdc = market.get("pdc")

    # NIFTY live refresh uses the same saved market security configuration.
    nifty_sid = market.get("security_id") or market.get("SecurityId")
    if nifty_sid:
        nq = client.quotes([nifty_sid], exchange_segment="IDX_I").get(int(nifty_sid))
        if nq:
            ltp = nq["ltp"]

    if ltp is not None and pdc:
        day_pct = (float(ltp) - float(pdc)) / float(pdc) * 100
    else:
        day_pct = market.get("day_pct", 0)

    mode = "BUY" if breadth_valid and day_pct > 0 and ad_ratio is not None and ad_ratio > 1 else ("SELL" if breadth_valid and day_pct < 0 and ad_ratio is not None and ad_ratio < 1 else "NEUTRAL")
    market.update({"ltp": ltp, "day_pct": day_pct, "ad_ratio": ad_ratio, "advances": advances, "declines": declines, "unchanged": unchanged, "valid_breadth_stocks": valid_count, "breadth_minimum": 1, "breadth_valid": breadth_valid, "sector_breadth": sector_breadth, "mode": mode})

    if "trend_runtime" not in st.session_state:
        persisted = load_persisted_trades()
        st.session_state.trend_runtime = persisted if persisted is not None else {"trades": [], "last_ltp": {}}
    runtime = st.session_state.trend_runtime
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
            trade.update({"status": "CLOSED", "exit_price": px, "exit_reason": reason, "exit_time": dt.strftime("%Y-%m-%d %H:%M:%S IST")})
    open_trades = [t for t in trades if t["status"] == "OPEN"]

    # New S1/S2/S3/S4 entries: maximum 4 positions total, one OPEN per strategy.
    # Every strategy may use ONLY the matching pre-qualified BUY/SELL stock set.
    if len(open_trades) < 4 and in_entry_window(dt) and mode in ("BUY", "SELL"):
        candidates = buy_rows if mode == "BUY" else sell_rows

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

        def open_position(strategy, side, stock, sid, q, sl, target):
            entry = float(q["ltp"])
            risk_per_share = (entry - sl) if side == "BUY" else (sl - entry)
            if risk_per_share <= 0:
                return False

            # Use the largest whole-share quantity that stays within the
            # maximum risk, thereby getting as close as possible to ₹1,500.
            quantity = int(MAX_RISK_PER_TRADE // risk_per_share)
            if quantity < 1:
                return False
            actual_risk = quantity * risk_per_share

            # Trades below ₹1,000 intended risk are not accepted.
            if actual_risk < MIN_RISK_PER_TRADE or actual_risk > MAX_RISK_PER_TRADE:
                return False
            trades.append({
                "strategy": strategy, "side": side, "status": "OPEN",
                "Symbol": stock["Symbol"], "SecurityId": sid,
                "Sector": str(stock.get("Sector") or stock.get("Industry") or "").strip(),
                "sector_ad": candidate_sector_ad(stock),
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
            entry_made = False
            open_strategies = {t["strategy"] for t in trades if t["status"] == "OPEN"}

            if mode == "BUY":
                # S1: Open > PDH, low 0.15% below PDH, reclaim PDH.
                s1 = q["open"] > pdh and q["low"] <= pdh * 0.9985 and prev_ltp is not None and prev_ltp < pdh and q["ltp"] >= pdh
                if s1 and "S1" not in open_strategies:
                    sl = (pdh + pdl) / 2
                    risk = q["ltp"] - sl
                    if risk > 0:
                        entry_made = open_position("S1", "BUY", stock, sid, q, sl, q["ltp"] + 1.25 * risk)

                # S2: Open between PDL/PDH, low 0.15% below PDL, reclaim PDL.
                if not entry_made:
                    s2 = pdl < q["open"] < pdh and q["low"] <= pdl * 0.9985 and prev_ltp is not None and prev_ltp < pdl and q["ltp"] >= pdl
                    if s2 and "S2" not in open_strategies:
                        sl = pdl - ((pdh - pdl) / 2)
                        risk = q["ltp"] - sl
                        if risk > 0:
                            entry_made = open_position("S2", "BUY", stock, sid, q, sl, q["ltp"] + 1.25 * risk)

                # S3: Open < PDL and reclaim PDL; SL = today's low; target = 1.25R.
                if not entry_made:
                    s3 = q["open"] < pdl and prev_ltp is not None and prev_ltp < pdl and q["ltp"] >= pdl
                    if s3 and "S3" not in open_strategies:
                        sl = q["low"]
                        risk = q["ltp"] - sl
                        if risk > 0:
                            entry_made = open_position("S3", "BUY", stock, sid, q, sl, q["ltp"] + 1.25 * risk)

                    # S4 BUY: open inside previous-day range, then break above PDH.
                    if not entry_made and "S4" not in open_strategies:
                        s4 = q["open"] > pdl and q["open"] < pdh and prev_ltp is not None and prev_ltp < pdh and q["ltp"] >= pdh
                        if s4:
                            sl = (pdh + pdl) / 2
                            risk = q["ltp"] - sl
                            if risk > 0:
                                entry_made = open_position("S4", "BUY", stock, sid, q, sl, q["ltp"] + 1.25 * risk)
            else:
                # S1: Open < PDL, high 0.15% above PDL, break below PDL.
                s1 = q["open"] < pdl and q["high"] >= pdl * 1.0015 and prev_ltp is not None and prev_ltp > pdl and q["ltp"] <= pdl
                if s1 and "S1" not in open_strategies:
                    sl = (pdh + pdl) / 2
                    risk = sl - q["ltp"]
                    if risk > 0:
                        entry_made = open_position("S1", "SELL", stock, sid, q, sl, q["ltp"] - 1.25 * risk)

                # S2: Open between PDL/PDH, high 0.15% above PDH, break below PDH.
                if not entry_made:
                    s2 = pdl < q["open"] < pdh and q["high"] >= pdh * 1.0015 and prev_ltp is not None and prev_ltp > pdh and q["ltp"] <= pdh
                    if s2 and "S2" not in open_strategies:
                        sl = pdh + ((pdh - pdl) / 2)
                        risk = sl - q["ltp"]
                        if risk > 0:
                            entry_made = open_position("S2", "SELL", stock, sid, q, sl, q["ltp"] - 1.25 * risk)

                # S3: Open > PDH and break below PDH; SL = today's high; target = 1.25R.
                if not entry_made:
                    s3 = q["open"] > pdh and prev_ltp is not None and prev_ltp > pdh and q["ltp"] <= pdh
                    if s3 and "S3" not in open_strategies:
                        sl = q["high"]
                        risk = sl - q["ltp"]
                        if risk > 0:
                            entry_made = open_position("S3", "SELL", stock, sid, q, sl, q["ltp"] - 1.25 * risk)

                    # S4 SELL: open inside previous-day range, then break below PDL.
                    if not entry_made and "S4" not in open_strategies:
                        s4 = q["open"] > pdl and q["open"] < pdh and prev_ltp is not None and prev_ltp > pdl and q["ltp"] <= pdl
                        if s4:
                            sl = (pdh + pdl) / 2
                            risk = sl - q["ltp"]
                            if risk > 0:
                                entry_made = open_position("S4", "SELL", stock, sid, q, sl, q["ltp"] - 1.25 * risk)

            runtime["last_ltp"][str(sid)] = q["ltp"]
            if entry_made:
                break

    # Expose the already-fetched quote batch for display-only live P&L.
    market["live_quotes"] = live_quotes
    persist_trades(runtime)
    return market, runtime["trades"]

# --------------------------- Dashboard ---------------------------
st.markdown("""
<style>
.block-container {max-width:1200px;padding-top:1rem;padding-bottom:2rem;}
div[data-testid="stExpander"] {border-radius:10px;}
</style>
""",unsafe_allow_html=True)

st.title("📈 NIFTY 500 Trend Bot")
st.caption("Live Dhan monitoring • S1 / S2 / S3 / S4 paper strategies")

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
        except Exception: px=e
    px=float(px or e)
    return (px-e)*q if t.get("side")=="BUY" else (e-px)*q

def capital(t):
    return float(t.get("entry_price",0) or 0)*float(t.get("quantity",0) or 0)

def trade_df(items):
    data=[]
    for t in items:
        data.append({
            "Date":str(t.get("entry_time",""))[:10],
            "Strategy":t.get("strategy","—"),"Side":t.get("side","—"),
            "Symbol":t.get("Symbol","—"),
            "Stock Name":t.get("Company") or t.get("Stock Name") or t.get("Symbol","—"),
            "Entry Time":t.get("entry_time","—"),"Entry":t.get("entry_price"),
            "Qty":t.get("quantity"),"Capital Used":capital(t),"SL":t.get("SL"),
            "Target":t.get("target"),"Status":t.get("status","—"),
            "Exit Time":t.get("exit_time","—"),"Exit Price":t.get("exit_price"),
            "Exit Reason":t.get("exit_reason","OPEN" if t.get("status")=="OPEN" else "—"),
            "P&L":pnl(t)
        })
    return pd.DataFrame(data)

def stats(items):
    closed=[t for t in items if t.get("status")=="CLOSED"]
    opened=[t for t in items if t.get("status")=="OPEN"]
    cp=[pnl(t) for t in closed]
    wins=sum(x>0 for x in cp); losses=sum(x<0 for x in cp)
    caps=[capital(t) for t in items if capital(t)>0]
    return {
        "taken":len(items),"open":len(opened),"closed":len(closed),
        "wins":wins,"losses":losses,"winpct":wins/len(closed)*100 if closed else 0,
        "realized":sum(cp),"live":sum(pnl(t) for t in opened),
        "maxcap":max(caps) if caps else 0,"mincap":min(caps) if caps else 0,
        "avgwin":sum(x for x in cp if x>0)/wins if wins else 0,
        "avgloss":sum(x for x in cp if x<0)/losses if losses else 0,
        "maxprofit":max(cp) if cp else 0,"maxloss":min(cp) if cp else 0
    }

# 1. LIVE MARKET
st.subheader("📊 Live Market")
st.dataframe(pd.DataFrame([{
    "Worker":live_status,
    "NIFTY 500 LTP":market.get("ltp","—"),
    "NIFTY % vs PDC":f"{float(market.get('day_pct',0) or 0):.2f}%",
    "NIFTY 500 A/D":market.get("ad_ratio","—"),
    "Bias":mode
}]),use_container_width=True,hide_index=True)

# 2. STRATEGY TABLE
st.divider(); st.subheader("🎯 S1 / S2 / S3 / S4")
rules=[
["S1","BUY","09:30–13:00","Open > PDH → pullback 0.15% → reclaim PDH","(PDH+PDL)/2","1.25R","SL / Target / 14:55"],
["S1","SELL","09:30–13:00","Open < PDL → pullback 0.15% → break PDL","(PDH+PDL)/2","1.25R","SL / Target / 14:55"],
["S2","BUY","09:30–13:00","Open inside range → below PDL 0.15% → reclaim PDL","PDL−(PDH−PDL)/2","1.25R","SL / Target / 14:55"],
["S2","SELL","09:30–13:00","Open inside range → above PDH 0.15% → break PDH","PDH+(PDH−PDL)/2","1.25R","SL / Target / 14:55"],
["S3","BUY","09:30–13:00","Open < PDL → reclaim PDL","Today's Low","1.25R","SL / Target / 14:55"],
["S3","SELL","09:30–13:00","Open > PDH → break below PDH","Today's High","1.25R","SL / Target / 14:55"],
["S4","BUY","09:30–13:00","Open inside range → cross PDH","(PDH+PDL)/2","1.25R","SL / Target / 14:55"],
["S4","SELL","09:30–13:00","Open inside range → cross PDL","(PDH+PDL)/2","1.25R","SL / Target / 14:55"]]
st.dataframe(pd.DataFrame(rules,columns=["Strategy","Side","Entry Time","Entry Reason","SL","Target","Exit"]),use_container_width=True,hide_index=True)

# 3. TODAY
today=now_ist().strftime("%Y-%m-%d")
today_items=[t for t in trades if str(t.get("entry_time","")).startswith(today)]
st.divider(); st.subheader("📅 Today's Performance")
tdf=trade_df(today_items)
if tdf.empty: st.caption("No trades taken today.")
else: st.dataframe(tdf,use_container_width=True,hide_index=True)
s=stats(today_items)
st.caption(f"Trades Taken: {s['taken']} • Open: {s['open']} • Closed: {s['closed']} • Wins: {s['wins']} • Losses: {s['losses']} • Win %: {s['winpct']:.2f}% • Realized P&L: ₹{s['realized']:,.2f} • Live P&L: ₹{s['live']:,.2f} • Total P&L: ₹{s['realized']+s['live']:,.2f} • Max Capital Used: ₹{s['maxcap']:,.2f} • Min Capital Used: ₹{s['mincap']:,.2f}")

# 4. ALL TRADES
st.divider(); st.subheader("📂 All Trades")
adf=trade_df(trades)
with st.expander(f"Show complete trade history ({len(trades)})",expanded=False):
    if adf.empty: st.caption("No paper trades yet.")
    else: st.dataframe(adf,use_container_width=True,hide_index=True)
cs=stats(trades)
st.markdown("**Cumulative Performance**")
st.caption(f"Total Trades: {cs['taken']} • Closed: {cs['closed']} • Open: {cs['open']} • Wins: {cs['wins']} • Losses: {cs['losses']} • Overall Win %: {cs['winpct']:.2f}% • Total Realized P&L: ₹{cs['realized']:,.2f} • Live P&L: ₹{cs['live']:,.2f} • Total P&L: ₹{cs['realized']+cs['live']:,.2f} • Average Win: ₹{cs['avgwin']:,.2f} • Average Loss: ₹{cs['avgloss']:,.2f} • Max Profit: ₹{cs['maxprofit']:,.2f} • Max Loss: ₹{cs['maxloss']:,.2f} • Max Capital Used: ₹{cs['maxcap']:,.2f} • Min Capital Used: ₹{cs['mincap']:,.2f}")

def show_set(title,rows,icon):
    with st.expander(f"{icon} {title} ({len(rows)})",expanded=False):
        if not rows: st.caption("No stocks in this set."); return
        df=pd.DataFrame(rows)
        preferred=["Symbol","Company","Sector","Trend","LTP","1Y Return %","6M Return %","1M Return %","1W Return %","1D Return %","PDH","PDL"]
        cols=[x for x in preferred if x in df.columns]
        st.dataframe(df[cols] if cols else df,use_container_width=True,hide_index=True)

# 5. BUY / SELL
st.divider(); st.subheader("Stock Sets")
show_set("BUY SET",buy_rows,"🟢")
show_set("SELL SET",sell_rows,"🔴")

# 6. SECTOR A/D
st.divider(); st.subheader("🏢 Sector A/D")
sb=market.get("sector_breadth",{})
if sb:
    sdf=pd.DataFrame([{"Sector":name,**vals} for name,vals in sb.items()])
    if "ad_ratio" in sdf.columns: sdf=sdf.sort_values("ad_ratio",ascending=False)
    st.dataframe(sdf,use_container_width=True,hide_index=True)
else: st.caption("Sector A/D data will appear with live valid quotes.")

# 7. MONTH-WISE
st.divider(); st.subheader("📥 Month-wise Cumulative Performance")
if not adf.empty:
    x=adf.copy(); x["Month"]=pd.to_datetime(x["Date"],errors="coerce").dt.strftime("%Y-%m")
    rows=[]
    for month,g in x.dropna(subset=["Month"]).groupby("Month"):
        closed=g[g["Status"]=="CLOSED"]; wins=int((closed["P&L"]>0).sum()); losses=int((closed["P&L"]<0).sum())
        rows.append({"Month":month,"Trades":len(g),"Wins":wins,"Losses":losses,"Win %":wins/len(closed)*100 if len(closed) else 0,
            "Realized P&L":float(closed["P&L"].sum()),"Live P&L":float(g[g["Status"]=="OPEN"]["P&L"].sum()),
            "Total P&L":float(g["P&L"].sum()),"Max Capital Used":float(g["Capital Used"].max()),"Min Capital Used":float(g["Capital Used"].min())})
    mdf=pd.DataFrame(rows)
    st.dataframe(mdf,use_container_width=True,hide_index=True)
    st.download_button("📥 Download Month-wise Performance CSV",mdf.to_csv(index=False).encode("utf-8"),"nifty500_trend_bot_monthly_performance.csv","text/csv")
else: st.caption("Month-wise data will build from persistent trade history.")

st.caption("Persistent trade history is retained separately. Dashboard layout changes do not change entry, exit, SL, target, timing, sector filtering or risk logic.")
