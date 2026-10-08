#!/usr/bin/env python3
"""Historischer OKX-Test: V4 nur LONG + Bullish Engulfing, mit 1H-Trend, OHNE RSI. Keine Live-Trades.

python scanner.py --days 30 --markets 100
python scanner.py --days 30 --markets 400 --fee-bps 5 --slippage-bps 2

Benutzt ausschließlich abgeschlossene Kerzen und bewertet offene Positionen
konservativ: Bei TP und SL in derselben Kerze gilt UNCLEAR.
"""
import argparse
import csv
import json
import math
import time
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

BASE = 'https://www.okx.com'
MEMES = {'DOGE','SHIB','PEPE','BONK','FLOKI','WIF','BOME','MEME','TURBO','NEIRO','BRETT','MOG','POPCAT','MEW','PONKE','SLERF','BABYDOGE','DOGS','CAT','HIPPO','PNUT','GOAT','ACT','MOODENG','TRUMP','MELANIA'}
STABLES = {'USDT','USDC','DAI','FDUSD','TUSD','USDE','PYUSD','USDS','BUSD','USD0','FRAX'}


def api(path, params, attempts=4):
    url = BASE + path + '?' + urllib.parse.urlencode(params)
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={'User-Agent':'Mozilla/5.0 V4V10Backtest/1.0'})
            with urllib.request.urlopen(req, timeout=25) as response:
                data = json.load(response)
            if data.get('code') != '0':
                raise RuntimeError(str(data))
            return data.get('data', [])
        except Exception:
            if attempt == attempts-1:
                raise
            time.sleep(0.5 * 2**attempt)


def markets(limit):
    inst = api('/api/v5/public/instruments', {'instType':'SWAP'})
    ticks = api('/api/v5/market/tickers', {'instType':'SWAP'})
    volume = {t['instId']:float(t.get('volCcy24h') or 0) for t in ticks}
    choices = []
    for x in inst:
        symbol = x.get('instId','')
        if (symbol.endswith('-USDT-SWAP') and x.get('state') == 'live'
            and symbol.split('-')[0] not in MEMES | STABLES and volume.get(symbol,0)>0):
            choices.append((symbol,volume[symbol]))
    return [x for x,_ in sorted(choices,key=lambda x:-x[1])[:limit]]


def history(symbol, bar, start_ms, end_ms, pause):
    """OKX history-candles: after = older than timestamp, max 100/page."""
    seen = {}
    cursor = end_ms + 1
    for _ in range(300):
        batch = api('/api/v5/market/history-candles', {'instId':symbol,'bar':bar,'after':str(cursor),'limit':'100'})
        if not batch:
            break
        oldest = cursor
        for c in batch:
            ts = int(c[0]); oldest = min(oldest,ts)
            if start_ms <= ts <= end_ms and len(c)>8 and c[8]=='1':
                seen[ts] = {'ts':ts,'open':float(c[1]),'high':float(c[2]),'low':float(c[3]),'close':float(c[4]),'volume':float(c[5])}
        if oldest >= cursor or oldest < start_ms:
            break
        cursor = oldest
        time.sleep(pause)
    return [seen[t] for t in sorted(seen)]


def ema_series(prices, n):
    out=[None]*len(prices)
    if len(prices)<n:return out
    value=sum(prices[:n])/n
    out[n-1]=value
    for i in range(n,len(prices)):
        value=value*(1-2/(n+1))+prices[i]*2/(n+1)
        out[i]=value
    return out


def atr(candles,i,n=14):
    trs=[]
    for j in range(max(1,i-n+1),i+1):
        c=candles[j]; prev=candles[j-1]
        trs.append(max(c['high']-c['low'],abs(c['high']-prev['close']),abs(c['low']-prev['close'])))
    return sum(trs)/len(trs) if trs else 0


def bullish(c):return c['close']>c['open']
def bearish(c):return c['close']<c['open']
def body(c):return abs(c['close']-c['open'])
def rng(c):return max(c['high']-c['low'],1e-12)

def reversal(candles,i,side,version):
    for j in range(i-2,i+1):
        c=candles[j]; p=candles[j-1]
        if side=='LONG':
            if bearish(p) and bullish(c) and c['open']<=p['close'] and c['close']>=p['open']:
                return 'Bullish Engulfing'
            if version=='V4':
                valid=(min(c['open'],c['close'])-c['low']>=max(2*body(c),.4*rng(c))
                    and c['high']-max(c['open'],c['close'])<=.25*rng(c)
                    and c['close']>=c['low']+.6*rng(c))
            else:
                valid=(min(c['open'],c['close'])>=c['high']-.382*rng(c)
                       and min(c['open'],c['close'])>c['low'])
            if valid:return 'Bullish Hammer'
        else:
            if bullish(p) and bearish(c) and c['open']>=p['close'] and c['close']<=p['open']:
                return 'Bearish Engulfing'
            if version=='V4':
                valid=(c['high']-max(c['open'],c['close'])>=max(2*body(c),.4*rng(c))
                    and min(c['open'],c['close'])-c['low']<=.25*rng(c)
                    and c['close']<=c['low']+.4*rng(c))
            else:
                valid=(max(c['open'],c['close'])<=c['low']+.382*rng(c)
                       and max(c['open'],c['close'])<c['high'])
            if valid:return 'Bearish Pinbar'
    return None


def trend_at(hourly, ts, min_bars=55):
    """Hourly candle opens at ts; only use bars closed by 15m signal close."""
    eligible=[c for c in hourly if c['ts']+3600000<=ts]
    if len(eligible)<min_bars:return 'NEUTRAL',None
    closes=[c['close'] for c in eligible]
    f=ema_series(closes,20)[-1];s=ema_series(closes,50)[-1]
    regime='BULLISH' if closes[-1]>f>s else 'BEARISH' if closes[-1]<f<s else 'NEUTRAL'
    momentum=(closes[-1]/closes[-4]-1)*100 if closes[-4] else 0
    btc='BULLISH' if regime=='BULLISH' and momentum>=.75 else 'BEARISH' if regime=='BEARISH' and momentum<=-.75 else 'NEUTRAL'
    return regime,btc


def evaluate(candles, entry_i, side, entry, stop, tp, fee_bps, slip_bps):
    risk=abs(entry-stop)
    if risk<=0:return 'INVALID',0,0
    # Signals are placed at completed 15m close. Start evaluating NEXT candle.
    for j in range(entry_i+1,len(candles)):
        c=candles[j]
        hit_stop=c['low']<=stop if side=='LONG' else c['high']>=stop
        hit_tp=c['high']>=tp if side=='LONG' else c['low']<=tp
        minutes=(c['ts']-candles[entry_i]['ts'])/60000
        if hit_stop and hit_tp:return 'UNCLEAR',None,minutes
        if hit_stop or hit_tp:
            gross=-1 if hit_stop else 2
            costs=2*entry*(fee_bps+slip_bps)/10000/risk
            return 'STOP' if hit_stop else 'TP2',gross-costs,minutes
    return 'OPEN',None,None


def backtest(candles,hourly,btc,version,args,diagnostics):
    results=[]; last_stop={}; active_until={}
    for i in range(55,len(candles)):
        c=candles[i]; ts=c['ts']+900000
        for side in ('LONG',):
            pattern=reversal(candles,i,side,version)
            if pattern != 'Bullish Engulfing':diagnostics['Kein Bullish Engulfing']+=1;continue
            structure=(c['close']>max(x['high'] for x in candles[i-3:i]) if side=='LONG'
                       else c['close']<min(x['low'] for x in candles[i-3:i]))
            if not structure:diagnostics['Strukturbruch']+=1;continue
            # Beide Varianten handeln nur in Richtung des bestaetigten 1H-Coin-Trends.
            trend,_=trend_at(hourly,ts)
            if trend!=('BULLISH' if side=='LONG' else 'BEARISH'):
                diagnostics['Coin-Trend']+=1;continue
            if version=='V10':
                _,btc_regime=trend_at(btc,ts)
                if btc_regime==('BEARISH' if side=='LONG' else 'BULLISH'):
                    diagnostics['BTC-Trend']+=1;continue
                if last_stop.get(side,0)>ts:
                    diagnostics['12h-Cooldown']+=1;continue
            if active_until.get(side,0)>ts:
                diagnostics['Offene Position gleicher Richtung']+=1;continue
            a=atr(candles,i)
            if a<=0:diagnostics['ATR']+=1;continue
            entry=c['close']
            stop=(min(x['low'] for x in candles[i-9:i+1])-.15*a if side=='LONG'
                  else max(x['high'] for x in candles[i-9:i+1])+.15*a)
            risk=abs(entry-stop)
            if risk<=0:diagnostics['Stop ungültig']+=1;continue
            if version=='V10' and side=='LONG' and risk/entry*100<.75:
                diagnostics['LONG Stop <0.75%']+=1;continue
            tp=entry+(2*risk if side=='LONG' else -2*risk)
            status,net_r,minutes=evaluate(candles,i,side,entry,stop,tp,args.fee_bps,args.slippage_bps)
            if status=='INVALID':continue
            diagnostics['SIGNAL']+=1
            if minutes is not None:active_until[side]=ts+minutes*60000
            else:active_until[side]=float('inf')
            if status=='STOP' and version=='V10' and minutes is not None:
                last_stop[side]=ts+minutes*60000+12*3600000
            results.append({'version':version,'symbol':args.symbol,'side':side,'signal_time':datetime.fromtimestamp(ts/1000,timezone.utc).isoformat(),
                'pattern':pattern,'entry':entry,'stop':stop,'tp2':tp,'status':status,'net_r':net_r,'duration_min':minutes})
    return results


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--days',type=int,default=30)
    p.add_argument('--markets',type=int,default=100)
    p.add_argument('--fee-bps',type=float,default=5,help='Gebühr pro Seite in Basispunkten')
    p.add_argument('--slippage-bps',type=float,default=2,help='Slippage pro Seite in Basispunkten')
    p.add_argument('--pause',type=float,default=.12)
    p.add_argument('--output',default='backtest_trades.csv')
    p.add_argument('--diagnostics',default='backtest_filters.csv')
    args=p.parse_args()
    if args.days<1 or not 1<=args.markets<=400:p.error('days >= 1 und markets 1..400 erforderlich')
    end=int(time.time()*1000)-3600000
    start=end-args.days*86400000
    warmup=start-5*86400000
    btc=[]  # Kein BTC-Filter fuer V4; spart API-Abfragen
    symbols=markets(args.markets)
    all_results=[]; all_counts=[]
    for index,symbol in enumerate(symbols,1):
        print(f'{index}/{len(symbols)} {symbol}',flush=True)
        try:
            candles=history(symbol,'15m',warmup,end,args.pause)
            hourly=history(symbol,'1H',warmup,end,args.pause)
            if len(candles)<60 or len(hourly)<55:
                print('  Übersprungen: zu wenig Daten');continue
            for version in ('V4',):
                counts=Counter()
                args.symbol=symbol
                trades=backtest(candles,hourly,btc,version,args,counts)
                # exclude warm-up signals; preserve all historical outcomes for in-window trades
                trades=[t for t in trades if datetime.fromisoformat(t['signal_time']).timestamp()*1000>=start]
                all_results.extend(trades)
                all_counts.append({'version':version,'symbol':symbol,**counts})
        except Exception as error:
            print('  FEHLER:',error,flush=True)
    fields=['version','symbol','side','signal_time','pattern','entry','stop','tp2','status','net_r','duration_min']
    with open(args.output,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(all_results)
    reasons=sorted({k for row in all_counts for k in row if k not in ('version','symbol')})
    with open(args.diagnostics,'w',newline='',encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=['version','symbol']+reasons);w.writeheader();w.writerows(all_counts)
    for version in ('V4',):
        trades=[x for x in all_results if x['version']==version]
        resolved=[x for x in trades if x['status'] in ('TP2','STOP')]
        wins=sum(x['status']=='TP2' for x in resolved)
        durations=[x['duration_min'] for x in resolved]
        print(f'{version}: {len(trades)} Signale | {len(resolved)} abgeschlossen | Trefferquote {100*wins/len(resolved):.1f}%' if resolved else f'{version}: {len(trades)} Signale, keine abgeschlossenen Trades')
        if resolved:
            print(f'  Netto R: {sum(x["net_r"] for x in resolved):.2f} | innerhalb 60 Min: {sum(d<=60 for d in durations)}/{len(durations)}')
    print('Ergebnisse:',args.output,'| Filter:',args.diagnostics)
    print('HINWEIS: Aktuelle Top-Märkte statt historischer Auswahl; kein Tick-Backtest, keine Garantie.')

if __name__=='__main__':
    main()
