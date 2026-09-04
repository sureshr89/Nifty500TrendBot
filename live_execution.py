"""Professional fail-closed Dhan live execution layer for NIFTY 500 S1."""
from __future__ import annotations
import math, os, time
from urllib.parse import quote
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

IST=ZoneInfo("Asia/Kolkata"); API="https://api.dhan.co/v2"
MAX_OPEN_POSITIONS=1; MAX_TRADES_PER_DAY=1; MAX_CAPITAL_PER_TRADE=150000.0
MIN_RISK_PER_TRADE=1000.0; MAX_RISK_PER_TRADE=1500.0
ENTRY_START="09:15"; ENTRY_END="13:00"; FORCE_EXIT_TIME="14:55"
LIVE_CONFIRMATION="I_UNDERSTAND_REAL_MONEY_RISK"

class LiveSafetyError(RuntimeError): pass

@dataclass(frozen=True)
class Sizing:
    quantity:int; risk_per_share:float; planned_risk:float; planned_capital:float

def calculate_quantity(entry, stop):
    entry=float(entry); stop=float(stop); rps=abs(entry-stop)
    if entry<=0 or rps<=0: raise LiveSafetyError("Invalid entry/stop")
    minq=math.ceil(MIN_RISK_PER_TRADE/rps); maxq=math.floor(MAX_RISK_PER_TRADE/rps)
    capq=math.floor(MAX_CAPITAL_PER_TRADE/entry); q=min(maxq,capq)
    if q<minq or q<1: raise LiveSafetyError("Cannot satisfy ₹1,000–₹1,500 risk band within ₹1.5 lakh cap")
    risk=q*rps; capital=q*entry
    if not (MIN_RISK_PER_TRADE<=risk<=MAX_RISK_PER_TRADE and capital<=MAX_CAPITAL_PER_TRADE): raise LiveSafetyError("Sizing safety check failed")
    return Sizing(q,rps,risk,capital)

def in_entry_window(now=None):
    return ENTRY_START <= (now or datetime.now(IST)).strftime("%H:%M") < ENTRY_END
def force_exit_due(now=None):
    return (now or datetime.now(IST)).strftime("%H:%M") >= FORCE_EXIT_TIME
def correlation_id(security_id, side):
    # Deterministic for the trading day: restart-safe duplicate protection.
    # If the process restarts, the same stock/side resolves to the same ID.
    return f"S1-{datetime.now(IST):%y%m%d}-{int(security_id)}-{str(side).upper()[0]}"

class DhanExecutionClient:
    def __init__(self):
        self.client_id=os.getenv("DHAN_CLIENT_ID","").strip(); self.token=os.getenv("DHAN_ACCESS_TOKEN","").strip()
        if not self.client_id or not self.token: raise LiveSafetyError("Dhan credentials missing")
        if os.getenv("LIVE_TRADING_ENABLED","false").lower()!="true": raise LiveSafetyError("LIVE_TRADING_ENABLED is not true")
        # Do not trust a manual boolean for real-money execution. Dhan readiness
        # below verifies the actual runtime egress IP against Dhan's configured
        # PRIMARY/SECONDARY IPs before any order is allowed.
        if os.getenv("LIVE_TRADING_CONFIRMATION","")!=LIVE_CONFIRMATION: raise LiveSafetyError("Real-money confirmation gate not satisfied")
        if os.getenv("LIVE_ACCOUNT_DEDICATED_TO_BOT","false").lower()!="true": raise LiveSafetyError("Dedicated bot-account gate not satisfied")
        self.headers={"access-token":self.token,"Content-Type":"application/json","Accept":"application/json"}
        self.proxy_host=os.getenv("PROXY_HOST","").strip()
        self.proxy_port=os.getenv("PROXY_PORT","443").strip()
        self.proxy_username=os.getenv("PROXY_USERNAME","").strip()
        self.proxy_password=os.getenv("PROXY_PASSWORD","").strip()
        self.proxy_ip=os.getenv("PROXY_IP","").strip()
        if not all((self.proxy_host, self.proxy_port, self.proxy_username, self.proxy_password, self.proxy_ip)):
            raise LiveSafetyError("Static proxy configuration missing")
        if not self.proxy_ip.count(".") == 3:
            raise LiveSafetyError("Configured proxy IP is invalid")
        proxy_url=(
            f"https://{quote(self.proxy_username, safe='')}:{quote(self.proxy_password, safe='')}"
            f"@{self.proxy_host}:{self.proxy_port}"
        )
        self.proxies={"http":proxy_url,"https":proxy_url}
        self.session=requests.Session()
        # Fail closed: never silently bypass the purchased static proxy.
        self.session.trust_env=False
    def request(self,method,path,payload=None,allow_not_found=False):
        r=self.session.request(method,API+path,headers=self.headers,json=payload,timeout=12,proxies=self.proxies)
        if allow_not_found and r.status_code == 404:
            return None
        if r.status_code>=400: raise LiveSafetyError(f"Dhan {method} {path} failed {r.status_code}: {r.text[:300]}")
        return r.json() if r.text.strip() else {}
    def profile(self):
        x=self.request("GET","/profile")
        return x if isinstance(x,dict) else {}

    def configured_ips(self):
        x=self.request("GET","/ip/getIP")
        return x if isinstance(x,dict) else {}

    def runtime_egress_ip(self):
        try:
            r=self.session.get("https://api.ipify.org", params={"format":"json"}, timeout=8, proxies=self.proxies)
            r.raise_for_status()
            ip=str((r.json() or {}).get("ip","")).strip()
            if not ip:
                raise LiveSafetyError("Unable to resolve runtime egress IP")
            return ip
        except LiveSafetyError:
            raise
        except Exception as exc:
            raise LiveSafetyError(f"Unable to resolve runtime egress IP: {exc}") from exc

    def live_readiness(self):
        """Broker-side readiness check. Never exposes credentials."""
        profile=self.profile()
        broker_client=str(profile.get("dhanClientId","")).strip()
        if broker_client and broker_client != self.client_id:
            raise LiveSafetyError("Dhan token belongs to a different client ID")
        ips=self.configured_ips()
        runtime_ip=self.runtime_egress_ip()
        approved={str(ips.get("primaryIP") or "").strip(), str(ips.get("secondaryIP") or "").strip()}
        approved.discard("")
        if self.proxy_ip not in approved:
            raise LiveSafetyError("Configured proxy IP is not one of the Dhan-approved static IPs")
        if runtime_ip != self.proxy_ip:
            raise LiveSafetyError(
                f"Proxy egress verification mismatch: expected {self.proxy_ip}, got {runtime_ip}"
            )
        if runtime_ip not in approved:
            raise LiveSafetyError(
                f"Runtime egress IP {runtime_ip} is not one of the Dhan-approved static IPs"
            )
        return {
            "status":"READY",
            "runtime_ip":runtime_ip,
            "primary_ip":ips.get("primaryIP"),
            "secondary_ip":ips.get("secondaryIP"),
            "token_validity":profile.get("tokenValidity"),
        }

    def positions(self):
        x=self.request("GET","/positions"); return x if isinstance(x,list) else []
    def orders(self):
        x=self.request("GET","/orders"); return x if isinstance(x,list) else []
    def super_orders(self):
        x=self.request("GET","/super/orders"); return x if isinstance(x,list) else []
    def active_bot_positions(self,owned_ids):
        owned={str(int(x)) for x in owned_ids}; out=[]
        for p in self.positions():
            if str(p.get("securityId","")) not in owned or str(p.get("productType","")).upper()!="INTRADAY": continue
            net=float(p.get("netQty",p.get("netQuantity",0)) or 0)
            if abs(net)>0: out.append(p)
        return out
    def has_pending_or_open_for_security(self,sid):
        sid=str(int(sid)); active={"TRANSIT","PENDING","PART_TRADED","TRIGGERED","INACTIVE"}
        if any(str(o.get("securityId",""))==sid and str(o.get("orderStatus","")).upper() in active for o in self.orders()): return True
        return bool(self.active_bot_positions({sid}))
    def assert_capacity(self, owned_ids):
        active=self.active_bot_positions(owned_ids)
        if len(active)>=MAX_OPEN_POSITIONS: raise LiveSafetyError(f"Maximum {MAX_OPEN_POSITIONS} live bot positions reached")
        return active
    def cancel_super_order_entry(self, order_id):
        return self.request("DELETE", f"/super/orders/{order_id}/ENTRY_LEG")

    def cancel_super_order_all_legs(self, order_id):
        # Best-effort emergency cancellation before flattening a partial fill.
        results = []
        for leg in ("ENTRY_LEG", "TARGET_LEG", "STOP_LOSS_LEG"):
            try:
                results.append(self.request("DELETE", f"/super/orders/{order_id}/{leg}"))
            except Exception:
                pass
        return results

    def emergency_flatten(self, sid, side, quantity, cid):
        if int(quantity) <= 0:
            return {"status": "NO_FILL"}
        reverse = "SELL" if str(side).upper() == "BUY" else "BUY"
        return self.request("POST", "/orders", {
            "dhanClientId": self.client_id,
            "correlationId": f"{cid}-FLAT"[:30],
            "transactionType": reverse,
            "exchangeSegment": "NSE_EQ",
            "productType": "INTRADAY",
            "orderType": "MARKET",
            "securityId": str(int(sid)),
            "quantity": int(quantity),
            "price": 0,
            "validity": "DAY",
            "afterMarketOrder": False,
        })

    def place_super_order(self,sid,side,quantity,entry,target,stop,cid,owned_ids):
        if force_exit_due(): raise LiveSafetyError("Force-exit time reached")
        if not in_entry_window(): raise LiveSafetyError("Outside entry window")
        # Real broker-side readiness: token/client identity and the actual
        # runtime egress IP must match one of the IPs configured at Dhan.
        self.live_readiness()
        # Restart-safe idempotency: query correlation ID before any new order.
        existing = self.get_order_by_correlation(cid)
        if existing:
            return existing
        self.assert_daily_trade_limit()
        self.assert_capacity(owned_ids)
        if self.has_pending_or_open_for_security(sid): raise LiveSafetyError("Duplicate/open/pending order blocked")
        return self.request("POST","/super/orders",{"dhanClientId":self.client_id,"correlationId":cid,"transactionType":side,"exchangeSegment":"NSE_EQ","productType":"INTRADAY","orderType":"LIMIT","securityId":str(int(sid)),"quantity":int(quantity),"price":float(entry),"targetPrice":float(target),"stopLossPrice":float(stop),"trailingJump":0.0})
    def place_market_order(self, sid, side, quantity, cid):
        """Explicit one-time market order path for a manually triggered execution test."""
        if int(quantity) != 1:
            raise LiveSafetyError("One-time test is restricted to exactly 1 share")
        if str(side).upper() not in {"BUY", "SELL"}:
            raise LiveSafetyError("Invalid market-order side")
        self.live_readiness()
        existing = self.get_order_by_correlation(cid)
        if existing:
            return existing
        # Match Dhan v2's regular-order request structure explicitly. Keeping
        # zero-valued order fields in the payload avoids broker-side validation
        # failures caused by omitted disclosed/trigger fields.
        return self.request("POST", "/orders", {
            "dhanClientId": self.client_id,
            "correlationId": str(cid)[:30],
            "transactionType": str(side).upper(),
            "exchangeSegment": "NSE_EQ",
            "productType": "INTRADAY",
            "orderType": "MARKET",
            "validity": "DAY",
            "securityId": str(int(sid)),
            "quantity": 1,
            # Dhan's regular MARKET order contract expects these as numeric
            # fields. Do not send Super/BO-only fields on /orders.
            "disclosedQuantity": 0,
            "price": 0,
            "triggerPrice": 0,
            "afterMarketOrder": False,
            "amoTime": "OPEN",
        })

    def regular_order_by_id(self, order_id):
        x = self.request("GET", f"/orders/{order_id}")
        return x if isinstance(x, dict) else {}

    def get_order_by_correlation(self,cid):
        return self.request("GET",f"/orders/external/{cid}",allow_not_found=True)

    def bot_super_orders_today(self):
        prefix = f"S1-{datetime.now(IST):%y%m%d}-"
        return [
            o for o in self.super_orders()
            if str(o.get("correlationId", "")).startswith(prefix)
        ]

    def assert_daily_trade_limit(self):
        # Dhan's Super Order book is for the current trading day. Count every
        # bot entry attempt with today's deterministic S1 correlation prefix,
        # including rejected/cancelled attempts, so the bot can never keep
        # retrying into multiple real entries after a restart.
        if len(self.bot_super_orders_today()) >= MAX_TRADES_PER_DAY:
            raise LiveSafetyError(f"Maximum {MAX_TRADES_PER_DAY} live bot trade per day reached")
    def confirm_super_order(self,order_id,timeout_seconds=30):
        # Never mark a trade open merely because Dhan accepted the request.
        deadline=time.time()+timeout_seconds; terminal={"REJECTED","CANCELLED","EXPIRED"}
        last=None
        while time.time()<deadline:
            for o in self.super_orders():
                if str(o.get("orderId"))==str(order_id):
                    last=o; status=str(o.get("orderStatus","")).upper()
                    if status in terminal: raise LiveSafetyError(f"Order ended {status}: {o}")
                    filled=int(o.get("filledQty",0) or 0)
                    if status=="TRADED" and filled>0:
                        return o
                    if status=="PART_TRADED" and filled>0:
                        self.cancel_super_order_all_legs(order_id)
                        sid = o.get("securityId")
                        side = o.get("transactionType")
                        cid = str(o.get("correlationId") or f"S1-PART-{order_id}")
                        self.emergency_flatten(sid, side, filled, cid)
                        raise LiveSafetyError("Partial fill was emergency-flattened; no trade recorded")
            time.sleep(1)
        if last and str(last.get("orderStatus","")).upper() in {"PENDING","TRANSIT","PART_TRADED"}:
            try:
                self.cancel_super_order_all_legs(order_id)
                filled=int(last.get("filledQty",0) or 0)
                if filled>0:
                    self.emergency_flatten(last.get("securityId"), last.get("transactionType"), filled, str(last.get("correlationId") or f"S1-PART-{order_id}"))
            except Exception:
                pass
        raise LiveSafetyError(f"Entry was not fully filled and confirmed within {timeout_seconds}s")

    def exit_all_intraday_safely(self,owned_ids):
        # Dhan's endpoint exits ALL account positions/orders. Refuse unless this
        # is a dedicated bot account and the caller explicitly opts in.
        if os.getenv("LIVE_ACCOUNT_DEDICATED_TO_BOT","false").lower()!="true":
            raise LiveSafetyError("Refusing Exit-All: account is not explicitly marked dedicated to this bot")
        if not self.active_bot_positions(owned_ids): return {"status":"NO_BOT_POSITIONS"}
        return self.request("DELETE","/positions")

def prepare_signal(side,entry,stop):
    side=side.upper(); entry=float(entry); stop=float(stop)
    if side=="BUY" and not entry>stop: raise LiveSafetyError("BUY requires entry > SL")
    if side=="SELL" and not stop>entry: raise LiveSafetyError("SELL requires SL > entry")
    if side not in {"BUY","SELL"}: raise LiveSafetyError("Invalid side")
    s=calculate_quantity(entry,stop); target=entry+s.risk_per_share if side=="BUY" else entry-s.risk_per_share
    return s,target
