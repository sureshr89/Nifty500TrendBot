"""Professional fail-closed Dhan live execution layer for NIFTY 500 S1."""
from __future__ import annotations
import math, os, time, uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

IST=ZoneInfo("Asia/Kolkata"); API="https://api.dhan.co/v2"
MAX_OPEN_POSITIONS=2; MAX_CAPITAL_PER_TRADE=150000.0
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
    return f"S1-{datetime.now(IST):%y%m%d}-{security_id}-{side[0]}-{uuid.uuid4().hex[:5]}"

class DhanExecutionClient:
    def __init__(self):
        self.client_id=os.getenv("DHAN_CLIENT_ID","").strip(); self.token=os.getenv("DHAN_ACCESS_TOKEN","").strip()
        if not self.client_id or not self.token: raise LiveSafetyError("Dhan credentials missing")
        if os.getenv("LIVE_TRADING_ENABLED","false").lower()!="true": raise LiveSafetyError("LIVE_TRADING_ENABLED is not true")
        if os.getenv("LIVE_STATIC_IP_APPROVED","false").lower()!="true": raise LiveSafetyError("Static-IP execution not approved")
        if os.getenv("LIVE_TRADING_CONFIRMATION","")!=LIVE_CONFIRMATION: raise LiveSafetyError("Real-money confirmation gate not satisfied")
        self.headers={"access-token":self.token,"Content-Type":"application/json","Accept":"application/json"}
    def request(self,method,path,payload=None):
        r=requests.request(method,API+path,headers=self.headers,json=payload,timeout=12)
        if r.status_code>=400: raise LiveSafetyError(f"Dhan {method} {path} failed {r.status_code}: {r.text[:300]}")
        return r.json() if r.text.strip() else {}
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
    def place_super_order(self,sid,side,quantity,entry,target,stop,cid):
        if force_exit_due(): raise LiveSafetyError("Force-exit time reached")
        if not in_entry_window(): raise LiveSafetyError("Outside entry window")
        if self.has_pending_or_open_for_security(sid): raise LiveSafetyError("Duplicate/open/pending order blocked")
        return self.request("POST","/super/orders",{"dhanClientId":self.client_id,"correlationId":cid,"transactionType":side,"exchangeSegment":"NSE_EQ","productType":"INTRADAY","orderType":"LIMIT","securityId":str(int(sid)),"quantity":int(quantity),"price":float(entry),"targetPrice":float(target),"stopLossPrice":float(stop),"trailingJump":0.0})
    def confirm_super_order(self,order_id,timeout_seconds=20):
        deadline=time.time()+timeout_seconds; terminal={"REJECTED","CANCELLED","EXPIRED"}
        while time.time()<deadline:
            for o in self.super_orders():
                if str(o.get("orderId"))==str(order_id):
                    status=str(o.get("orderStatus","")).upper()
                    if status in terminal: raise LiveSafetyError(f"Order ended {status}: {o}")
                    return o
            time.sleep(1)
        raise LiveSafetyError("Unable to confirm super order")
    def exit_all_intraday_safely(self,owned_ids):
        if not self.active_bot_positions(owned_ids): return {"status":"NO_BOT_POSITIONS"}
        return self.request("DELETE","/positions")

def prepare_signal(side,entry,stop):
    side=side.upper(); entry=float(entry); stop=float(stop)
    if side=="BUY" and not entry>stop: raise LiveSafetyError("BUY requires entry > SL")
    if side=="SELL" and not stop>entry: raise LiveSafetyError("SELL requires SL > entry")
    if side not in {"BUY","SELL"}: raise LiveSafetyError("Invalid side")
    s=calculate_quantity(entry,stop); target=entry+s.risk_per_share if side=="BUY" else entry-s.risk_per_share
    return s,target
