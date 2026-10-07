import csv,json,os,time,urllib.parse,urllib.request
from concurrent.futures import ThreadPoolExecutor,as_completed
from datetime import datetime,timezone

STRATEGY="V8_STRUCTURE_PULLBACK"; JOURNAL="signals_v8.csv"; TEST_LIMIT=100
MAX_WORKERS=8; PULLBACK_LOOKBACK=6; STOP_BUFFER=.001
OKX="https://www.okx.com"; CG="https://api.coingecko.com/api/v3"
STABLE={"USDT","USDC","DAI","FDUSD","TUSD","USDE","USDS","PYUSD","USD1","FRAX","LUSD","USDP","RLUSD"}
MEME={"DOGE","SHIB","PEPE","BONK","WIF","FLOKI","BRETT","MOG","POPCAT","MEW","TURBO","NEIRO","BABYDOGE","MEME","BOME","PNUT","GOAT","ACT","PENGU","FARTCOIN","TRUMP","MELANIA","SPX"}
FIELDS=["time","strategy","symbol","side","timeframe","setup_type","entry","stop","tp_1r","status_1r","tp_15r","status_15r","tp_2r","status_2r","trigger","last_checked"]

def get(url,p=None):
    if p:url+="?"+urllib.parse.urlencode(p)
    last=None
    for n in range(3):
        try:
            q=urllib.request.Request(url,headers={"User-Agent":"CryptoScanner-V8","Accept":"application/json"})
            with urllib.request.urlopen(q,timeout=8) as r:return json.loads(r.read())
        except Exception as e:
            last=e; time.sleep(.4*(n+1))
    raise last

def instruments():
    d=get(OKX+"/api/v5/public/instruments",{"instType":"SWAP"}).get("data",[])
    out={}
    for x in d:
        i=x.get("instId",""); cat=str(x.get("instCategory",""))
        if x.get("state")=="live" and i.endswith("-USDT-SWAP") and cat=="1":
            out[i[:-10].upper()]=i
    return out

def cat_symbols(cat):
    s=set()
    for page in (1,2):
        try:
            d=get(CG+"/coins/markets",{"vs_currency":"usd","category":cat,"order":"market_cap_desc","per_page":250,"page":page,"sparkline":"false"})
            s|={str(x.get("symbol","")).upper() for x in d}
            if len(d)<250:break
        except Exception:break
    return s

def universe():
    inst=instruments(); top=[]
    for page in (1,2):
        try:
            top+=get(CG+"/coins/markets",{"vs_currency":"usd","order":"market_cap_desc","per_page":250,"page":page,"sparkline":"false"})
        except Exception as e: print("CoinGecko:",e)
    excluded=STABLE|MEME|cat_symbols("stablecoins")|cat_symbols("meme-token")
    out=[]; seen=set()
    for c in top[:500]:
        s=str(c.get("symbol","")).upper()
        if s and s not in excluded and s not in seen and s in inst:
            out.append(inst[s]); seen.add(s)
    print("Universe:",len(out),"Top-500 crypto swaps; meme/stable excluded")
    return out

def candles(inst,limit=180):
    d=get(OKX+"/api/v5/market/candles",{"instId":inst,"bar":"15m","limit":limit}).get("data",[])
    a=[{"ts":int(x[0]),"open":float(x[1]),"high":float(x[2]),"low":float(x[3]),"close":float(x[4])} for x in d if len(x)>8 and str(x[8])=="1"]
    return sorted(a,key=lambda x:x["ts"])

def pivots(d,span=2):
    hi=[]; lo=[]
    for i in range(span,len(d)-span):
        if d[i]["high"]>max(x["high"] for x in d[i-span:i]) and d[i]["high"]>=max(x["high"] for x in d[i+1:i+span+1]):hi.append((i,d[i]["high"]))
        if d[i]["low"]<min(x["low"] for x in d[i-span:i]) and d[i]["low"]<=min(x["low"] for x in d[i+1:i+span+1]):lo.append((i,d[i]["low"]))
    return hi,lo

def trend(d):
    h,l=pivots(d[:-1])
    if len(h)<2 or len(l)<2:return None
    if h[-1][1]>h[-2][1] and l[-1][1]>l[-2][1]:return "LONG"
    if h[-1][1]<h[-2][1] and l[-1][1]<l[-2][1]:return "SHORT"

def strong(c,side):
    r=max(c["high"]-c["low"],1e-12); b=abs(c["close"]-c["open"])
    if b/r<.60:return False
    return (side=="LONG" and c["close"]>c["open"] and c["close"]>=c["low"]+.75*r) or (side=="SHORT" and c["close"]<c["open"] and c["close"]<=c["low"]+.25*r)

def setup(d):
    """Pure price-action setup: trend -> structural pullback -> strong color confirmation."""
    if len(d)<80:return None
    side=trend(d)
    if not side or not strong(d[-1],side):return None

    c=d[-1]
    pb=d[-1-PULLBACK_LOOKBACK:-1]
    h,l=pivots(d[:-1])
    if len(h)<2 or len(l)<2:return None

    if side=="LONG":
        # Existing bullish structure must remain intact during the pullback.
        # Pullback = recent move down, followed by a strong GREEN candle.
        protected_low=l[-2][1]
        pullback_low=min(x["low"] for x in pb)
        had_retrace=any(pb[i]["close"] < pb[i-1]["close"] for i in range(1,len(pb)))
        if not had_retrace or pullback_low <= protected_low:return None
        # Confirmation must reclaim the previous candle high.
        if c["close"] <= pb[-1]["high"]:return None
        entry=c["close"]; stop=pullback_low*(1-STOP_BUFFER)
        trig="HH+HL uptrend + structural pullback + strong GREEN confirmation"
    else:
        # Existing bearish structure must remain intact during the pullback.
        # Pullback = recent move up, followed by a strong RED candle.
        protected_high=h[-2][1]
        pullback_high=max(x["high"] for x in pb)
        had_retrace=any(pb[i]["close"] > pb[i-1]["close"] for i in range(1,len(pb)))
        if not had_retrace or pullback_high >= protected_high:return None
        # Confirmation must break the previous candle low.
        if c["close"] >= pb[-1]["low"]:return None
        entry=c["close"]; stop=pullback_high*(1+STOP_BUFFER)
        trig="LH+LL downtrend + structural pullback + strong RED confirmation"

    risk=abs(entry-stop)
    if risk<=0:return None
    f=lambda rr: entry+risk*rr if side=="LONG" else entry-risk*rr
    return side,entry,stop,f(1),f(1.5),f(2),trig

def read():
    if not os.path.exists(JOURNAL):return []
    with open(JOURNAL,newline="",encoding="utf8") as f:return list(csv.DictReader(f))
def write(rows):
    with open(JOURNAL,"w",newline="",encoding="utf8") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS);w.writeheader();w.writerows(rows)
def fmt(x):return f"{x:.10f}".rstrip("0").rstrip(".")

def update_one(r):
    try:
        cs=[c for c in candles(r["symbol"],100) if c["ts"]>int(r.get("last_checked") or 0)]
        for sk,tk in (("status_1r","tp_1r"),("status_15r","tp_15r"),("status_2r","tp_2r")):
            if r[sk]!="OPEN":continue
            sl=float(r["stop"]); tp=float(r[tk])
            for c in cs:
                hs=(c["low"]<=sl if r["side"]=="LONG" else c["high"]>=sl)
                ht=(c["high"]>=tp if r["side"]=="LONG" else c["low"]<=tp)
                if hs and ht:r[sk]="UNCLEAR";break
                if hs:r[sk]="STOP";break
                if ht:r[sk]="TP";break
        if cs:r["last_checked"]=str(cs[-1]["ts"])
    except Exception as e:print("update",r["symbol"],e)
    return r

def scan(i):
    try:
        d=candles(i);return i,d[-1]["ts"],setup(d),None
    except Exception as e:return i,0,None,str(e)

def tg(s):
    t=os.getenv("TELEGRAM_BOT_TOKEN",""); ch=os.getenv("TELEGRAM_CHAT_ID","")
    if not t or not ch:return
    try:
        data=urllib.parse.urlencode({"chat_id":ch,"text":s}).encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{t}/sendMessage",data=data),timeout=8)
    except Exception as e:print("telegram",e)

def main():
    rows=read()
    opens=[r for r in rows if "OPEN" in (r["status_1r"],r["status_15r"],r["status_2r"])]
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:list(ex.map(update_one,opens))
    if len(rows)>=TEST_LIMIT:write(rows);print("100 V8 trades reached");return
    syms=universe(); existing={(r["symbol"],r["time"]) for r in rows}; new=[]; errors=0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        fs=[ex.submit(scan,s) for s in syms]
        for f in as_completed(fs):
            i,ts,s,err=f.result()
            if err:errors+=1;continue
            if not s or len(rows)+len(new)>=TEST_LIMIT:continue
            side,en,sl,t1,t15,t2,tr=s; tm=datetime.fromtimestamp(ts/1000,tz=timezone.utc).isoformat()
            if (i,tm) in existing:continue
            r={"time":tm,"strategy":STRATEGY,"symbol":i,"side":side,"timeframe":"15m","setup_type":"Trend + Pullback + Confirmation","entry":fmt(en),"stop":fmt(sl),"tp_1r":fmt(t1),"status_1r":"OPEN","tp_15r":fmt(t15),"status_15r":"OPEN","tp_2r":fmt(t2),"status_2r":"OPEN","trigger":tr,"last_checked":str(ts)}
            new.append(r);existing.add((i,tm))
    rows+=new;write(rows)
    for r in new:tg(f"V8 {r['side']} {r['symbol']}\n{r['trigger']}\nEntry {r['entry']} | SL {r['stop']}\nTP 1R {r['tp_1r']} | 1.5R {r['tp_15r']} | 2R {r['tp_2r']}")
    print("checked",len(syms),"errors",errors,"new",len(new),"total",len(rows))
if __name__=="__main__":main()
