import csv,json,os,time,urllib.parse,urllib.request
from datetime import datetime,timezone
BASE="https://fapi.binance.com"; TFS=("15m","30m","1h"); JOURNAL="signals.csv"
FIELDS=["time","symbol","side","timeframe","setup_type","entry","stop","tp1","tp2","tp3","planned_rr","trigger","status","last_checked"]
def api(p,q=None):
 u=BASE+p+("?" + urllib.parse.urlencode(q) if q else "")
 with urllib.request.urlopen(urllib.request.Request(u,headers={"User-Agent":"crypto-scanner"}),timeout=20) as r:return json.loads(r.read())
def ks(s,t,n=150):
 return [[int(x[0]),*map(float,x[1:6])] for x in api("/fapi/v1/klines",{"symbol":s,"interval":t,"limit":n})[:-1]]
def ema(v,n):
 a=2/(n+1);e=v[0]
 for x in v[1:]:e=a*x+(1-a)*e
 return e
def tr(r):
 c=[x[4] for x in r];a,b=ema(c[-60:],20),ema(c[-90:],50)
 return 1 if c[-1]>a>b else -1 if c[-1]<a<b else 0
def atr(r,n=14):
 z=[]
 for i in range(1,len(r)):
  h,l,p=r[i][2],r[i][3],r[i-1][4];z.append(max(h-l,abs(h-p),abs(l-p)))
 return sum(z[-n:])/n
def universe():
 info=api("/fapi/v1/exchangeInfo"); ok={x["symbol"] for x in info["symbols"] if x.get("status")=="TRADING" and x.get("quoteAsset")=="USDT" and x.get("contractType")=="PERPETUAL"}
 stable={"USDC","FDUSD","TUSD","USDP","DAI","BUSD"};a=[]
 for x in api("/fapi/v1/ticker/24hr"):
  s=x["symbol"];q=float(x.get("quoteVolume",0))
  if s in ok and s.endswith("USDT") and s[:-4] not in stable and q>=1e7:a.append((s,q))
 return [s for s,q in sorted(a,key=lambda x:x[1],reverse=True)[:100]]
def sig(s):
 d={t:ks(s,t) for t in TFS};b=tr(d["1h"])
 if not b or b!=tr(d["30m"]):return
 r=d["15m"];o,h,l,c,v=r[-1][1:6];pc=r[-2][4];A=atr(r);body=abs(c-o);rg=max(h-l,1e-12);av=sum(x[5] for x in r[-21:-1])/20
 side=None
 if b==1 and c>o and min(o,c)-l>=max(body,rg*.28) and c>pc and v>=av*.9:side="LONG"
 if b==-1 and c<o and h-max(o,c)>=max(body,rg*.28) and c<pc and v>=av*.9:side="SHORT"
 if not side:return
 entry=c
 if side=="LONG":stop=min(l,entry-1.15*A);risk=entry-stop;t=[entry+1.5*risk,entry+2*risk,entry+3*risk]
 else:stop=max(h,entry+1.15*A);risk=stop-entry;t=[entry-1.5*risk,entry-2*risk,entry-3*risk]
 now=datetime.now(timezone.utc).isoformat(timespec="seconds")
 return dict(time=now,symbol=s,side=side,timeframe="15m",setup_type="Rejection/Reclaim + 30m/1h trend",entry=entry,stop=stop,tp1=t[0],tp2=t[1],tp3=t[2],planned_rr=2,trigger="closed 15m rejection + volume + 30m/1h alignment",status="OPEN",last_checked=now)
def load():
 if not os.path.exists(JOURNAL):return []
 with open(JOURNAL,newline="",encoding="utf8") as f:return list(csv.DictReader(f))
def save(a):
 with open(JOURNAL,"w",newline="",encoding="utf8") as f:w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(a)
def update(a):
 cache={}
 for x in a:
  if x["status"] not in ("OPEN","TP1"):continue
  try:
   s=x["symbol"];cache.setdefault(s,ks(s,"15m",100));since=datetime.fromisoformat(x["last_checked"]).timestamp()*1000
   st,t1,t2,t3=map(float,[x["stop"],x["tp1"],x["tp2"],x["tp3"]]);status=x["status"]
   for k in cache[s]:
    if k[0]<=since:continue
    hi,lo=k[2],k[3];sh=lo<=st if x["side"]=="LONG" else hi>=st;target=None
    if x["side"]=="LONG":
     if hi>=t3:target="TP3"
     elif hi>=t2:target="TP2"
     elif hi>=t1:target="TP1"
    else:
     if lo<=t3:target="TP3"
     elif lo<=t2:target="TP2"
     elif lo<=t1:target="TP1"
    if sh and target:status="UNCLEAR";break
    if sh:status="STOP";break
    if target:status=target
   x["status"]=status;x["last_checked"]=datetime.now(timezone.utc).isoformat(timespec="seconds")
  except Exception as e:print("journal",e)
 return a
def main():
 a=update(load());active={(x["symbol"],x["side"]) for x in a if x["status"] in ("OPEN","TP1")}
 for s in universe():
  try:
   x=sig(s)
   if x and (s,x["side"]) not in active:a.append(x);active.add((s,x["side"]));print("SIGNAL",json.dumps(x))
   time.sleep(.03)
  except Exception as e:print("WARN",s,e)
 save(a)
if __name__=="__main__":main()
