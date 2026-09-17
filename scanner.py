import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://www.okx.com"
JOURNAL = "signals.csv"
MAX_MARKETS = 100
PAUSE = 0.04

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FIELDS = [
    "time", "strategy", "symbol", "side", "timeframe", "setup_type",
    "entry", "stop", "tp1", "tp2", "tp3", "planned_rr",
    "trigger", "status", "last_checked"
]


def now():
    return datetime.now(timezone.utc).isoformat()


def num(x):
    return format(float(x), ".12g")


def flt(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def telegram(text):
    if not TOKEN or not CHAT_ID:
        print("Telegram: Secrets fehlen")
        return False
    try:
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": "true"
        }).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data=data,
            headers={"User-Agent": "CryptoScanner/2.0"}
        )
        with urllib.request.urlopen(req, timeout=20) as r:
            result = json.loads(r.read().decode())
        if not result.get("ok"):
            raise RuntimeError(result)
        print("Telegram: Nachricht gesendet")
        return True
    except Exception as e:
        print("TELEGRAM ERROR:", e)
        return False


def api(path, params=None):
    if params:
        path += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        BASE + path, headers={"User-Agent": "Mozilla/5.0 CryptoScanner/2.0"}
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        obj = json.loads(r.read().decode())
    if obj.get("code") != "0":
        raise RuntimeError(obj)
    return obj.get("data", [])


def markets():
    instruments = api("/api/v5/public/instruments", {"instType": "SWAP"})
    tickers = api("/api/v5/market/tickers", {"instType": "SWAP"})
    vols = {x.get("instId", ""): flt(x.get("volCcy24h")) for x in tickers}
    excluded = {"USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "PYUSD", "USDS", "BUSD"}
    out = []
    for x in instruments:
        s = x.get("instId", "")
        base = s.split("-")[0] if s else ""
        if x.get("state") == "live" and s.endswith("-USDT-SWAP") and base not in excluded:
            out.append((s, vols.get(s, 0)))
    out.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in out[:MAX_MARKETS]]


def candles(symbol, bar, limit=120):
    raw = api("/api/v5/market/candles", {
        "instId": symbol, "bar": bar, "limit": str(limit)
    })
    out = []
    for x in reversed(raw):
        if len(x) > 8 and x[8] != "1":
            continue
        out.append({
            "ts": int(x[0]), "open": float(x[1]), "high": float(x[2]),
            "low": float(x[3]), "close": float(x[4]), "volume": float(x[5])
        })
    return out


def ema(values, period):
    if not values:
        return []
    a = 2 / (period + 1)
    out = [values[0]]
    for x in values[1:]:
        out.append(a * x + (1 - a) * out[-1])
    return out


def atr(d, period=14):
    if len(d) < 2:
        return 0
    tr = []
    for i in range(1, len(d)):
        tr.append(max(
            d[i]["high"] - d[i]["low"],
            abs(d[i]["high"] - d[i-1]["close"]),
            abs(d[i]["low"] - d[i-1]["close"])
        ))
    v = tr[-period:]
    return sum(v) / len(v) if v else 0


def avgvol(d, period=20):
    v = [x["volume"] for x in d[-period:]]
    return sum(v) / len(v) if v else 0


def body(c):
    return abs(c["close"] - c["open"])


def rng(c):
    return max(c["high"] - c["low"], 1e-12)


def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def bull(c):
    return c["close"] > c["open"]


def bear(c):
    return c["close"] < c["open"]


def bull_engulf(p, c):
    return bear(p) and bull(c) and c["open"] <= p["close"] and c["close"] >= p["open"]


def bear_engulf(p, c):
    return bull(p) and bear(c) and c["open"] >= p["close"] and c["close"] <= p["open"]


def bull_reject(c):
    return lower_wick(c) >= max(body(c)*1.5, rng(c)*0.30) and c["close"] >= c["low"] + rng(c)*0.55


def bear_reject(c):
    return upper_wick(c) >= max(body(c)*1.5, rng(c)*0.30) and c["close"] <= c["low"] + rng(c)*0.45


def bull_outside(p, c):
    return c["high"] > p["high"] and c["low"] < p["low"] and bull(c)


def bear_outside(p, c):
    return c["high"] > p["high"] and c["low"] < p["low"] and bear(c)


def displacement(d, side):
    if len(d) < 10:
        return False
    avgb = sum(body(x) for x in d[-10:-1]) / 9
    if side == "LONG":
        return bull(d[-1]) and body(d[-1]) >= avgb*1.5
    return bear(d[-1]) and body(d[-1]) >= avgb*1.5


# ---------- CURRENT ----------

def current_bias(d):
    if len(d) < 50:
        return "NEUTRAL"
    closes = [x["close"] for x in d]
    e20, e50 = ema(closes, 20)[-1], ema(closes, 50)[-1]
    if closes[-1] > e20 > e50:
        return "LONG"
    if closes[-1] < e20 < e50:
        return "SHORT"
    return "NEUTRAL"


def current_setup(d):
    if len(d) < 25:
        return None
    c, p = d[-1], d[-2]
    hist = d[-22:-2]
    res = max(x["high"] for x in hist)
    sup = min(x["low"] for x in hist)
    tol = atr(d)*0.35

    if c["close"] > res and bull(c):
        return "LONG", "Breakout"
    if c["close"] < sup and bear(c):
        return "SHORT", "Breakdown"
    if c["low"] < sup and c["close"] > sup and bull(c):
        return "LONG", "Liquidity Sweep/Reclaim"
    if c["high"] > res and c["close"] < res and bear(c):
        return "SHORT", "Liquidity Sweep/Reclaim"
    if abs(c["low"]-sup) <= tol and (bull_reject(c) or bull_engulf(p,c)):
        return "LONG", "Support Rejection"
    if abs(c["high"]-res) <= tol and (bear_reject(c) or bear_engulf(p,c)):
        return "SHORT", "Resistance Rejection"
    return None


def current_trigger(d, side):
    if len(d) < 25:
        return None
    c, p = d[-1], d[-2]
    av = avgvol(d[:-1])
    vol_ok = av > 0 and c["volume"] >= av*0.8
    recent = d[-8:-1]

    if side == "LONG":
        pat = bull_engulf(p,c) or bull_reject(c) or bull_outside(p,c) or displacement(d,"LONG")
        brk = c["close"] > max(x["high"] for x in recent)
        if pat and (brk or vol_ok):
            return "Bullish candle + 15m structure/momentum"
    else:
        pat = bear_engulf(p,c) or bear_reject(c) or bear_outside(p,c) or displacement(d,"SHORT")
        brk = c["close"] < min(x["low"] for x in recent)
        if pat and (brk or vol_ok):
            return "Bearish candle + 15m structure/momentum"
    return None


# ---------- CREAMER_CRYPTO ----------

def environment(d):
    if len(d) < 55:
        return "NEUTRAL", 0, 0
    closes = [x["close"] for x in d]
    e20 = ema(closes,20)
    e50 = ema(closes,50)
    ls = ss = 0
    if closes[-1] > e20[-1]: ls += 1
    else: ss += 1
    if e20[-1] > e50[-1]: ls += 1
    elif e20[-1] < e50[-1]: ss += 1
    if e20[-1] > e20[-5]: ls += 1
    elif e20[-1] < e20[-5]: ss += 1

    old, new = d[-24:-12], d[-12:]
    if max(x["high"] for x in new) > max(x["high"] for x in old) and min(x["low"] for x in new) > min(x["low"] for x in old):
        ls += 2
    elif max(x["high"] for x in new) < max(x["high"] for x in old) and min(x["low"] for x in new) < min(x["low"] for x in old):
        ss += 2

    bias = "LONG" if ls >= ss+2 else "SHORT" if ss >= ls+2 else "NEUTRAL"
    return bias, ls, ss


def profile(d, bins=24):
    sample = d[-72:]
    if len(sample) < 30:
        return None
    lo, hi = min(x["low"] for x in sample), max(x["high"] for x in sample)
    width = hi-lo
    if width <= 0:
        return None
    b = [0.0]*bins
    for c in sample:
        price = (c["high"]+c["low"]+c["close"])/3
        i = min(bins-1, max(0, int((price-lo)/width*bins)))
        b[i] += c["volume"]
    total = sum(b)
    if total <= 0:
        return None
    ranked = sorted(range(bins), key=lambda i:b[i], reverse=True)
    chosen, running = [], 0
    for i in ranked:
        chosen.append(i); running += b[i]
        if running >= total*0.70:
            break
    step = width/bins
    return {
        "val": lo + min(chosen)*step,
        "vah": lo + (max(chosen)+1)*step,
        "mid": (lo+hi)/2
    }


def creamer_setup(d, env):
    if len(d) < 75:
        return None
    vp = profile(d)
    if not vp:
        return None

    c,p = d[-1],d[-2]
    hist=d[-22:-2]
    sup=min(x["low"] for x in hist)
    res=max(x["high"] for x in hist)
    a=atr(d)
    if a<=0:
        return None
    tol=a*0.45
    av=avgvol(d[:-1])
    vr=c["volume"]/av if av else 0
    long_events=[]
    short_events=[]

    if c["low"] < sup and c["close"] > sup:
        long_events.append("downside sweep/reclaim")
    if c["high"] > res and c["close"] < res:
        short_events.append("upside sweep/reclaim")
    if p["close"] < sup and c["close"] > sup:
        long_events.append("failed breakdown")
    if p["close"] > res and c["close"] < res:
        short_events.append("failed breakout")
    if (c["low"] <= vp["val"]+tol) and bull_reject(c):
        long_events.append("VAL/discount rejection")
    if (c["high"] >= vp["vah"]-tol) and bear_reject(c):
        short_events.append("VAH/premium rejection")
    if p["close"] > res and c["low"] <= res+tol and c["close"] > res and bull(c):
        long_events.append("breakout retest")
    if p["close"] < sup and c["high"] >= sup-tol and c["close"] < sup and bear(c):
        short_events.append("breakdown retest")

    options=[]
    if long_events and (c["close"] <= vp["mid"] or c["low"] <= vp["val"]+tol):
        score=2+min(2,len(long_events))+(1 if env=="LONG" else -1 if env=="SHORT" else 0)+(1 if vr>=1 else 0)
        options.append(("LONG",score,long_events))
    if short_events and (c["close"] >= vp["mid"] or c["high"] >= vp["vah"]-tol):
        score=2+min(2,len(short_events))+(1 if env=="SHORT" else -1 if env=="LONG" else 0)+(1 if vr>=1 else 0)
        options.append(("SHORT",score,short_events))

    if not options:
        return None
    side,score,events=max(options,key=lambda x:x[1])
    if score < 3:
        return None
    return side, " + ".join(events[:2]), score


def creamer_trigger(d, side):
    if len(d)<30:
        return None
    c,p=d[-1],d[-2]
    av=avgvol(d[:-1])
    vr=c["volume"]/av if av else 0
    recent=d[-8:-1]
    labels=[]
    score=0

    if side=="LONG":
        absorb=vr>=1.15 and lower_wick(c)/rng(c)>=0.35 and c["close"]>=c["low"]+rng(c)*0.60
        failed=c["low"]<p["low"] and c["close"]>p["low"] and bull(c)
        pattern=bull_engulf(p,c) or bull_reject(c) or bull_outside(p,c)
        brk=c["close"]>max(x["high"] for x in recent)
    else:
        absorb=vr>=1.15 and upper_wick(c)/rng(c)>=0.35 and c["close"]<=c["low"]+rng(c)*0.40
        failed=c["high"]>p["high"] and c["close"]<p["high"] and bear(c)
        pattern=bear_engulf(p,c) or bear_reject(c) or bear_outside(p,c)
        brk=c["close"]<min(x["low"] for x in recent)

    if absorb: score+=2; labels.append("absorption proxy")
    if failed: score+=2; labels.append("failed auction/reclaim")
    if pattern: score+=1; labels.append("reaction candle")
    if displacement(d,side): score+=1; labels.append("displacement")
    if brk: score+=2; labels.append("15m structure break")
    if vr>=1: score+=1; labels.append("volume confirmation")

    if score>=4 and (absorb or failed or brk):
        return ", ".join(labels[:4]) + f" | score {score}"
    return None


def trade(strategy,symbol,side,setup,trigger,d):
    entry=d[-1]["close"]
    a=atr(d)
    if a<=0: return None
    recent=d[-8:]
    if side=="LONG":
        stop=min(min(x["low"] for x in recent)-a*0.10, entry-a*0.80)
        risk=entry-stop
        if risk<=0:return None
        t1,t2,t3=entry+risk*1.25,entry+risk*2,entry+risk*3
    else:
        stop=max(max(x["high"] for x in recent)+a*0.10, entry+a*0.80)
        risk=stop-entry
        if risk<=0:return None
        t1,t2,t3=entry-risk*1.25,entry-risk*2,entry-risk*3
    t=now()
    return {
        "time":t,"strategy":strategy,"symbol":symbol,"side":side,
        "timeframe":"1h/30m/15m","setup_type":setup,
        "entry":num(entry),"stop":num(stop),"tp1":num(t1),"tp2":num(t2),"tp3":num(t3),
        "planned_rr":"2","trigger":trigger,"status":"OPEN","last_checked":t
    }


def load():
    if not os.path.exists(JOURNAL):
        return []
    with open(JOURNAL,newline="",encoding="utf-8") as f:
        rows=list(csv.DictReader(f))
    for r in rows:
        if not r.get("strategy"):
            r["strategy"]="CURRENT"
    return rows


def save(rows):
    with open(JOURNAL,"w",newline="",encoding="utf-8") as f:
        w=csv.DictWriter(f,fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k:r.get(k,"") for k in FIELDS})


def duplicate(rows,strategy,symbol,side):
    for r in rows:
        if (r.get("strategy","CURRENT")==strategy and r.get("symbol")==symbol
            and r.get("side")==side and r.get("status") in {"OPEN","TP1","TP2"}):
            return True
    return False


def deutsch(text):
    text = str(text)
    ersetzungen = [
        ("CREAMER_CRYPTO", "CREAMER-STRATEGIE"),
        ("CURRENT", "BISHERIGE STRATEGIE"),
        ("Liquidity Sweep/Reclaim", "LiquiditÃ¤ts-Sweep + RÃ¼ckeroberung"),
        ("Support Rejection", "Abweisung am Support"),
        ("Resistance Rejection", "Abweisung am Widerstand"),
        ("failed auction/reclaim", "Fehlausbruch + RÃ¼ckeroberung"),
        ("downside sweep/reclaim", "LiquiditÃ¤ts-Sweep unten + RÃ¼ckeroberung"),
        ("upside sweep/reclaim", "LiquiditÃ¤ts-Sweep oben + RÃ¼ckeroberung"),
        ("failed breakdown", "Fehlausbruch nach unten"),
        ("failed breakout", "Fehlausbruch nach oben"),
        ("VAL/discount rejection", "Abweisung in der gÃ¼nstigen Value-Zone"),
        ("VAH/premium rejection", "Abweisung in der teuren Value-Zone"),
        ("breakout retest", "Ausbruch nach oben + RÃ¼cktest"),
        ("breakdown retest", "Ausbruch nach unten + RÃ¼cktest"),
        ("Bullish candle + 15m structure/momentum", "Bullische Kerze + 15m Struktur/Momentum"),
        ("Bearish candle + 15m structure/momentum", "BÃ¤rische Kerze + 15m Struktur/Momentum"),
        ("absorption proxy", "Absorptions-BestÃ¤tigung"),
        ("reaction candle", "Reaktionskerze"),
        ("displacement", "starke Impulskerze"),
        ("15m structure break", "15m Strukturbruch"),
        ("volume confirmation", "VolumenbestÃ¤tigung"),
        ("location score", "Zonen-Score"),
        ("Breakdown", "Ausbruch nach unten"),
        ("Breakout", "Ausbruch nach oben"),
        ("NEUTRAL", "NEUTRAL"),
        ("LONG", "LONG"),
        ("SHORT", "SHORT"),
        ("OPEN", "OFFEN"),
        ("UNCLEAR", "UNKLAR"),
        ("TP1_THEN_STOP", "TP1 ERREICHT â DANACH STOP"),
        ("TP2_THEN_STOP", "TP2 ERREICHT â DANACH STOP"),
        ("STOP", "STOP-LOSS"),
    ]
    for a, b in ersetzungen:
        text = text.replace(a, b)
    return text


def strategie_name(name):
    return "Creamer-Strategie" if name == "CREAMER_CRYPTO" else "Bisherige Strategie"


def notify_trade(t):
    richtung = "ð¢ LONG" if t["side"] == "LONG" else "ð´ SHORT"
    telegram(
        f"ð¨ NEUES SIGNAL â {strategie_name(t['strategy'])}\n\n"
        f"Paar: {t['symbol']}\n"
        f"Richtung: {richtung}\n"
        f"Zeitrahmen: {t['timeframe']}\n"
        f"Setup: {deutsch(t['setup_type'])}\n"
        f"BestÃ¤tigung: {deutsch(t['trigger'])}\n\n"
        f"Einstieg: {t['entry']}\n"
        f"Stop-Loss: {t['stop']}\n"
        f"Ziel 1: {t['tp1']}\n"
        f"Ziel 2: {t['tp2']}\n"
        f"Ziel 3: {t['tp3']}\n"
        f"CRV bis Ziel 2: 1:2"
    )


def update_journal(rows):
    changes=[]
    terminal={"STOP","TP3","UNCLEAR","TP1_THEN_STOP","TP2_THEN_STOP"}
    for r in rows:
        if r.get("status") in terminal:
            continue
        if r.get("status") not in {"OPEN","TP1","TP2"}:
            continue
        try:
            start=int(datetime.fromisoformat(r["time"].replace("Z","+00:00")).timestamp()*1000)
            d=[c for c in candles(r["symbol"],"15m",100) if c["ts"]>start]
            old=r["status"]
            progress={"OPEN":0,"TP1":1,"TP2":2}.get(old,0)
            stop,tp1,tp2,tp3=map(flt,[r["stop"],r["tp1"],r["tp2"],r["tp3"]])
            status=old
            for c in d:
                if r["side"]=="LONG":
                    sh=c["low"]<=stop
                    hits=[c["high"]>=tp1,c["high"]>=tp2,c["high"]>=tp3]
                else:
                    sh=c["high"]>=stop
                    hits=[c["low"]<=tp1,c["low"]<=tp2,c["low"]<=tp3]
                high=3 if hits[2] else 2 if hits[1] else 1 if hits[0] else 0
                if sh and high>progress:
                    status="UNCLEAR"; break
                if high>progress:
                    progress=high
                    status={1:"TP1",2:"TP2",3:"TP3"}[progress]
                    if progress==3: break
                if sh:
                    status="STOP" if progress==0 else f"TP{progress}_THEN_STOP"
                    break
            r["status"]=status
            r["last_checked"]=now()
            if status!=old:
                msg=(
                    f"ð SIGNAL-UPDATE â {strategie_name(r.get('strategy','CURRENT'))}\n\n"
                    f"Paar: {r['symbol']}\n"
                    f"Richtung: {r['side']}\n"
                    f"Status: {deutsch(old)} â {deutsch(status)}"
                )
                changes.append(msg); telegram(msg)
        except Exception as e:
            print("Journal error",r.get("symbol"),e)
        time.sleep(PAUSE)
    return changes


def scan(symbol,rows):
    h1=candles(symbol,"1H",100); time.sleep(PAUSE)
    m30=candles(symbol,"30m",120); time.sleep(PAUSE)
    m15=candles(symbol,"15m",120)
    out=[]

    s=current_setup(m30)
    if s:
        side,stype=s
        b=current_bias(h1)
        if b in {"NEUTRAL",side}:
            trig=current_trigger(m15,side)
            if trig and not duplicate(rows,"CURRENT",symbol,side):
                t=trade("CURRENT",symbol,side,stype,trig,m15)
                if t: out.append(t)

    env,ls,ss=environment(h1)
    cs=creamer_setup(m30,env)
    if cs:
        side,stype,score=cs
        trig=creamer_trigger(m15,side)
        if trig and not duplicate(rows,"CREAMER_CRYPTO",symbol,side):
            setup=f"{stype} | 1H env={env} L{ls}/S{ss} | location score {score}"
            t=trade("CREAMER_CRYPTO",symbol,side,setup,trig,m15)
            if t: out.append(t)
    return out


def main():
    print("Crypto Scanner gestartet")
    rows=load()
    changes=update_journal(rows)

    try:
        ms=markets()
    except Exception as e:
        telegram(f"â Scanner Fehler: Market-Liste nicht geladen\n{e}")
        save(rows)
        raise

    print("OKX markets selected:",len(ms))
    scanned=errors=current_n=creamer_n=0

    for i,symbol in enumerate(ms,1):
        try:
            found=scan(symbol,rows)
            scanned+=1
            for t in found:
                rows.append(t)
                if t["strategy"]=="CURRENT": current_n+=1
                else: creamer_n+=1
                print(f"NEW [{t['strategy']}]: {symbol} {t['side']}")
                notify_trade(t)
        except Exception as e:
            errors+=1
            print("ERROR",symbol,e)
        if i%10==0:
            print(f"Progress: {i}/{len(ms)}")
        time.sleep(PAUSE)

    save(rows)
    print("Successfully scanned:",scanned)
    print("Errors:",errors)
    print("CURRENT setups:",current_n)
    print("CREAMER_CRYPTO setups:",creamer_n)
    print("Journal updates:",len(changes))

    if os.environ.get("GITHUB_EVENT_NAME")=="workflow_dispatch":
        telegram(
            "â CRYPTO-SCANNER AKTIV\n\n"
            f"MÃ¤rkte geprÃ¼ft: {scanned}/{len(ms)}\n"
            f"Fehler: {errors}\n"
            f"Neue Signale â Bisherige Strategie: {current_n}\n"
            f"Neue Signale â Creamer-Strategie: {creamer_n}\n"
            f"Journal-Aktualisierungen: {len(changes)}"
        )


if __name__=="__main__":
    main()
