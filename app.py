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

def clean_trade_history(trades):
    """Keep only trades matching the current clean sizing rules."""
    cleaned = []
    for t in trades or []:
        try:
            entry = float(t.get("entry_price", 0) or 0)
            qty = float(t.get("quantity", 0) or 0)
            rps = abs(float(t.get("risk_per_share", 0) or 0))
            cap = entry * qty
            risk = rps * qty
            sl = float(t.get("SL"))
            target = float(t.get("target"))
            reward = (target - entry) if t.get("side") == "BUY" else (entry - target)
            rr = reward / rps if rps > 0 else -1
            if (entry > 0 and qty > 0 and cap <= 150000.0 + 1e-6 and
                    1000.0 - 1e-6 <= risk <= 1500.0 + 1e-6 and
                    abs(rr - 1.125) < 1e-6):
                cleaned.append(t)
        except Exception:
            pass
    return cleaned

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
        persisted = persisted if persisted is not None else {"trades": [], "last_ltp": {}}
        original_trades = persisted.get("trades", [])
        persisted["trades"] = clean_trade_history(original_trades)
        st.session_state.trend_runtime = persisted
        if len(persisted["trades"]) != len(original_trades):
            persist_trades(persisted)
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
            configured_exit = float(trade["SL"] if reason == "STOP_LOSS" else trade["target"])
            trade.update({
                "status": "CLOSED",
                "exit_price": configured_exit,
                "trigger_ltp": float(px),
                "exit_reason": reason,
                "exit_time": dt.strftime("%Y-%m-%d %H:%M:%S IST")
            })
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
        TARGET_R_MULTIPLE = 1.125  # Reward = 1.125 × initial risk (RR 1:1.125)

        def open_position(strategy, side, stock, sid, q, sl, target):
            entry = float(q["ltp"])
            risk_per_share = (entry - sl) if side == "BUY" else (sl - entry)
            if risk_per_share <= 0:
                return False

            # Position size must satisfy BOTH the SL-risk band and the
            # maximum capital deployed per trade.
            MAX_CAPITAL_PER_TRADE = 150000.0
            risk_qty = int(MAX_RISK_PER_TRADE // risk_per_share)
            capital_qty = int(MAX_CAPITAL_PER_TRADE // entry)
            quantity = min(risk_qty, capital_qty)
            if quantity < 1:
                return False
            actual_risk = quantity * risk_per_share
            actual_capital = quantity * entry

            # Reject trades that cannot satisfy all sizing rules. Never force
            # a trade with risk below ₹1,000 just because the capital cap binds.
            if (actual_risk < MIN_RISK_PER_TRADE or
                    actual_risk > MAX_RISK_PER_TRADE or
                    actual_capital > MAX_CAPITAL_PER_TRADE):
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
                        entry_made = open_position("S1", "BUY", stock, sid, q, sl, q["ltp"] + TARGET_R_MULTIPLE * risk)

                # S2: Open between PDL/PDH, low 0.15% below PDL, reclaim PDL.
                if not entry_made:
                    s2 = pdl < q["open"] < pdh and q["low"] <= pdl * 0.9985 and prev_ltp is not None and prev_ltp < pdl and q["ltp"] >= pdl
                    if s2 and "S2" not in open_strategies:
                        sl = pdl - ((pdh - pdl) / 2)
                        risk = q["ltp"] - sl
                        if risk > 0:
                            entry_made = open_position("S2", "BUY", stock, sid, q, sl, q["ltp"] + TARGET_R_MULTIPLE * risk)

                # S3: Open < PDL and reclaim PDL; SL = today's low; target = 1.125R.
                if not entry_made:
                    s3 = q["open"] < pdl and prev_ltp is not None and prev_ltp < pdl and q["ltp"] >= pdl
                    if s3 and "S3" not in open_strategies:
                        sl = q["low"]
                        risk = q["ltp"] - sl
                        if risk > 0:
                            entry_made = open_position("S3", "BUY", stock, sid, q, sl, q["ltp"] + TARGET_R_MULTIPLE * risk)

                    # S4 BUY: open inside previous-day range, then break above PDH.
                    if not entry_made and "S4" not in open_strategies:
                        s4 = q["open"] > pdl and q["open"] < pdh and prev_ltp is not None and prev_ltp < pdh and q["ltp"] >= pdh
                        if s4:
                            sl = (pdh + pdl) / 2
                            risk = q["ltp"] - sl
                            if risk > 0:
                                entry_made = open_position("S4", "BUY", stock, sid, q, sl, q["ltp"] + TARGET_R_MULTIPLE * risk)
            else:
                # S1: Open < PDL, high 0.15% above PDL, break below PDL.
                s1 = q["open"] < pdl and q["high"] >= pdl * 1.0015 and prev_ltp is not None and prev_ltp > pdl and q["ltp"] <= pdl
                if s1 and "S1" not in open_strategies:
                    sl = (pdh + pdl) / 2
                    risk = sl - q["ltp"]
                    if risk > 0:
                        entry_made = open_position("S1", "SELL", stock, sid, q, sl, q["ltp"] - TARGET_R_MULTIPLE * risk)

                # S2: Open between PDL/PDH, high 0.15% above PDH, break below PDH.
                if not entry_made:
                    s2 = pdl < q["open"] < pdh and q["high"] >= pdh * 1.0015 and prev_ltp is not None and prev_ltp > pdh and q["ltp"] <= pdh
                    if s2 and "S2" not in open_strategies:
                        sl = pdh + ((pdh - pdl) / 2)
                        risk = sl - q["ltp"]
                        if risk > 0:
                            entry_made = open_position("S2", "SELL", stock, sid, q, sl, q["ltp"] - TARGET_R_MULTIPLE * risk)

                # S3: Open > PDH and break below PDH; SL = today's high; target = 1.125R.
                if not entry_made:
                    s3 = q["open"] > pdh and prev_ltp is not None and prev_ltp > pdh and q["ltp"] <= pdh
                    if s3 and "S3" not in open_strategies:
                        sl = q["high"]
                        risk = sl - q["ltp"]
                        if risk > 0:
                            entry_made = open_position("S3", "SELL", stock, sid, q, sl, q["ltp"] - TARGET_R_MULTIPLE * risk)

                    # S4 SELL: open inside previous-day range, then break below PDL.
                    if not entry_made and "S4" not in open_strategies:
                        s4 = q["open"] > pdl and q["open"] < pdh and prev_ltp is not None and prev_ltp > pdl and q["ltp"] <= pdl
                        if s4:
                            sl = (pdh + pdl) / 2
                            risk = sl - q["ltp"]
                            if risk > 0:
                                entry_made = open_position("S4", "SELL", stock, sid, q, sl, q["ltp"] - TARGET_R_MULTIPLE * risk)

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
    return {
        "taken":len(items),"open":len(opened),"closed":len(closed),
        "wins":wins,"losses":losses,"winpct":wins/len(closed)*100 if closed else 0,
        "realized":sum(cp),"live":sum(pnl(t) for t in opened),
        "maxcap":max(caps) if caps else 0,"mincap":min(caps) if caps else 0,
        "avgwin":sum(x for x in cp if x>0)/wins if wins else 0,
        "avgloss":sum(x for x in cp if x<0)/losses if losses else 0,
        "maxprofit":max(cp) if cp else 0,"maxloss":min(cp) if cp else 0,
        "grossprofit":sum(x for x in cp if x>0),"grossloss":sum(x for x in cp if x<0),
        "profitfactor":(sum(x for x in cp if x>0)/abs(sum(x for x in cp if x<0))) if sum(x for x in cp if x<0)!=0 else 0,
        "expectancy":sum(cp)/len(cp) if cp else 0
    }

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
st.dataframe(pd.DataFrame([{
    "Worker":live_status,
    "NIFTY 500 LTP":market.get("ltp","—"),
    "NIFTY % vs PDC":f"{float(market.get('day_pct',0) or 0):.2f}%",
    "NIFTY 500 A/D":market.get("ad_ratio","—"),
    "Bias":mode
}]),use_container_width=True,hide_index=True)

# 2 STRATEGY REFERENCE - COLLAPSIBLE
st.divider()
with st.expander("🎯 S1 / S2 / S3 / S4 — Strategy Rules",expanded=False):
    rules=[
    ["S1","BUY","09:30–13:00","Open > PDH → low ≤ PDH×0.9985 → reclaim PDH","(PDH+PDL)/2","1.125R","SL / Target / 14:55"],
    ["S1","SELL","09:30–13:00","Open < PDL → high ≥ PDL×1.0015 → break PDL","(PDH+PDL)/2","1.125R","SL / Target / 14:55"],
    ["S2","BUY","09:30–13:00","Open inside range → low ≤ PDL×0.9985 → reclaim PDL","PDL−(PDH−PDL)/2","1.125R","SL / Target / 14:55"],
    ["S2","SELL","09:30–13:00","Open inside range → high ≥ PDH×1.0015 → break PDH","PDH+(PDH−PDL)/2","1.125R","SL / Target / 14:55"],
    ["S3","BUY","09:30–13:00","Open < PDL → previous LTP < PDL → reclaim PDL","Today's Low","1.125R","SL / Target / 14:55"],
    ["S3","SELL","09:30–13:00","Open > PDH → previous LTP > PDH → break below PDH","Today's High","1.125R","SL / Target / 14:55"],
    ["S4","BUY","09:30–13:00","Open inside range → previous LTP < PDH → cross PDH","(PDH+PDL)/2","1.125R","SL / Target / 14:55"],
    ["S4","SELL","09:30–13:00","Open inside range → previous LTP > PDL → cross PDL","(PDH+PDL)/2","1.125R","SL / Target / 14:55"]]
    st.dataframe(pd.DataFrame(rules,columns=["Strategy","Side","Entry Window","Exact Entry Condition","SL","Target","Exit"]),use_container_width=True,hide_index=True)

# DATA
today=now_ist().strftime("%Y-%m-%d")
today_items=[t for t in trades if str(t.get("entry_time","")).startswith(today)]
tdf=trade_df(today_items)
adf=trade_df(trades)
s=stats(today_items)
cs=stats(trades)

# 3 TODAY PERFORMANCE — SUMMARY FIRST, TRADE TABLE SEPARATE COLLAPSE
st.divider()
st.subheader("📅 Today's Performance")

# Always-visible summary first for quick mobile viewing
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
with st.expander(f"Show Complete Trade History ({len(trades)} trades)",expanded=False):
    if adf.empty:
        st.caption("No paper trades yet.")
    else:
        st.dataframe(adf,use_container_width=True,hide_index=True)

st.markdown("**Cumulative Performance**")
cards([
    ("Total Trades",cs["taken"]),("Closed",cs["closed"]),("Open",cs["open"]),
    ("Wins",cs["wins"]),("Losses",cs["losses"]),("Overall Win %",f"{cs['winpct']:.2f}%"),
    ("Realized P&L",f"₹{cs['realized']:,.2f}"),("Live P&L",f"₹{cs['live']:,.2f}"),("Total P&L",f"₹{cs['realized']+cs['live']:,.2f}"),
    ("Average Win",f"₹{cs['avgwin']:,.2f}"),("Average Loss",f"₹{cs['avgloss']:,.2f}"),("Profit Factor",f"{cs['profitfactor']:.2f}"),
    ("Expectancy / Closed Trade",f"₹{cs['expectancy']:,.2f}"),("Max Profit",f"₹{cs['maxprofit']:,.2f}"),("Max Loss",f"₹{cs['maxloss']:,.2f}"),
    ("Gross Profit",f"₹{cs['grossprofit']:,.2f}"),("Gross Loss",f"₹{cs['grossloss']:,.2f}"),
    ("Max Capital Used",f"₹{cs['maxcap']:,.2f}"),("Min Capital Used",f"₹{cs['mincap']:,.2f}")
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
        sdf=pd.DataFrame([{"Sector":name,**vals} for name,vals in sb.items()])
        if "ad_ratio" in sdf.columns: sdf=sdf.sort_values("ad_ratio",ascending=False)
        st.dataframe(sdf,use_container_width=True,hide_index=True)
    else:
        st.caption("Sector A/D data will appear with live valid quotes.")

# 7 LIGHTWEIGHT EOD / BACKTEST DOWNLOAD
# Keep the website fast: analysis is exported to one Excel workbook and is not
# rendered as multiple heavy tables in the Streamlit page.
st.divider()
with st.expander("📥 EOD / Full 360° Analysis Download",expanded=False):
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
        export_buffer=io.BytesIO()
        with pd.ExcelWriter(export_buffer,engine="openpyxl") as writer:
            x.drop(columns=["Entry DateTime","Exit DateTime"],errors="ignore").to_excel(writer,index=False,sheet_name="All Trades")
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
