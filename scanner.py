#!/usr/bin/env python3
"""OKX historical 15m backtest: V4 baseline vs Connors RSI(2) variants with fixed stop.

Run: python scanner.py --days 90 --markets 100
No orders are placed. OHLC backtest, not live performance.
"""
import argparse
import csv
import json
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
            request = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 ScannerBacktest/2.0'})
            with urllib.request.urlopen(request, timeout=25) as response:
                data = json.load(response)
            if data.get('code') != '0':
                raise RuntimeError(str(data))
            return data.get('data', [])
        except Exception:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5 * 2 ** attempt)


def markets(limit):
    instruments = api('/api/v5/public/instruments', {'instType': 'SWAP'})
    tickers = api('/api/v5/market/tickers', {'instType': 'SWAP'})
    volumes = {t['instId']: float(t.get('volCcy24h') or 0) for t in tickers}
    eligible = []
    for instrument in instruments:
        symbol = instrument.get('instId', '')
        if (symbol.endswith('-USDT-SWAP') and instrument.get('state') == 'live'
                and symbol.split('-')[0] not in MEMES | STABLES and volumes.get(symbol, 0) > 0):
            eligible.append((symbol, volumes[symbol]))
    return [symbol for symbol, _ in sorted(eligible, key=lambda item: -item[1])[:limit]]


def history(symbol, bar, start_ms, end_ms, pause):
    """Fetch only confirmed OKX candles. 'after' paginates toward older candles."""
    found = {}
    cursor = end_ms + 1
    for _ in range(1600):
        batch = api('/api/v5/market/history-candles',
                    {'instId': symbol, 'bar': bar, 'after': str(cursor), 'limit': '100'})
        if not batch:
            break
        oldest = cursor
        for candle in batch:
            timestamp = int(candle[0])
            oldest = min(oldest, timestamp)
            if start_ms <= timestamp <= end_ms and len(candle) > 8 and candle[8] == '1':
                found[timestamp] = {
                    'ts': timestamp, 'open': float(candle[1]), 'high': float(candle[2]),
                    'low': float(candle[3]), 'close': float(candle[4]),
                    'volume': float(candle[5]),
                }
        if oldest >= cursor or oldest < start_ms:
            break
        cursor = oldest
        time.sleep(pause)
    return [found[timestamp] for timestamp in sorted(found)]


def ema_series(prices, length):
    values = [None] * len(prices)
    if len(prices) < length:
        return values
    value = sum(prices[:length]) / length
    values[length - 1] = value
    multiplier = 2 / (length + 1)
    for index in range(length, len(prices)):
        value += multiplier * (prices[index] - value)
        values[index] = value
    return values


def atr(candles, index, length=14):
    ranges = []
    for j in range(max(1, index - length + 1), index + 1):
        current, previous = candles[j], candles[j - 1]
        ranges.append(max(current['high'] - current['low'],
                          abs(current['high'] - previous['close']),
                          abs(current['low'] - previous['close'])))
    return sum(ranges) / len(ranges) if ranges else 0


def bullish_engulfing(previous, current):
    return (previous['close'] < previous['open'] and current['close'] > current['open']
            and current['open'] <= previous['close'] and current['close'] >= previous['open'])


def baseline_pattern(candles, index):
    # Retain original V4 detection: engulfing within the last three 15m candles.
    for j in range(index - 2, index + 1):
        if bullish_engulfing(candles[j - 1], candles[j]):
            return True
    return False


def hourly_trends(hourly):
    closes = [candle['close'] for candle in hourly]
    ema20 = ema_series(closes, 20)
    ema50 = ema_series(closes, 50)
    ema200 = ema_series(closes, 200)
    return ema20, ema50, ema200


def evaluate(candles, index, entry, stop, target, fee_bps, slippage_bps):
    risk = entry - stop
    if risk <= 0:
        return 'INVALID', None, None, None
    for j in range(index + 1, len(candles)):
        candle = candles[j]
        hit_stop = candle['low'] <= stop
        hit_target = candle['high'] >= target
        minutes = (candle['ts'] - candles[index]['ts']) / 60000
        if hit_stop and hit_target:
            return 'UNCLEAR', None, minutes, candle['ts'] + 900000
        if hit_stop or hit_target:
            # Conservative stop-first ambiguity; costs approximate two executions.
            gross_r = -1 if hit_stop else 2
            cost_r = 2 * entry * (fee_bps + slippage_bps) / 10000 / risk
            return ('STOP' if hit_stop else 'TP2'), gross_r - cost_r, minutes, candle['ts'] + 900000
    return 'OPEN', None, None, None


def backtest(candles, hourly, version, symbol, args, diagnostics):
    trades = []
    active_until = 0
    h20, h50, h200 = hourly_trends(hourly)
    # At signal close, only completed hourly candles are eligible.
    hour_index = -1
    for i in range(55, len(candles)):
        candle = candles[i]
        signal_close = candle['ts'] + 900000
        while (hour_index + 1 < len(hourly)
               and hourly[hour_index + 1]['ts'] + 3600000 <= signal_close):
            hour_index += 1
        if hour_index < 54:
            diagnostics['1H warmup'] += 1
            continue
        if active_until == float('inf') or signal_close < active_until:
            diagnostics['Position already open'] += 1
            continue
        # V4 and V4B share EXACTLY the same original signal filters.
        if not baseline_pattern(candles, i):
            diagnostics['No engulfing'] += 1
            continue
        if not candle['close'] > max(x['high'] for x in candles[i - 3:i]):
            diagnostics['No structure break'] += 1
            continue
        if not (h20[hour_index] is not None and h50[hour_index] is not None
                and hourly[hour_index]['close'] > h20[hour_index] > h50[hour_index]):
            diagnostics['No V4 uptrend'] += 1
            continue
        pattern = 'Bullish Engulfing'
        current_atr = atr(candles, i)
        if current_atr <= 0:
            diagnostics['Invalid ATR'] += 1
            continue
        entry = candle['close']
        fill_i = i
        if version == 'V4B':
            # After the confirmed signal, place a limit buy 0.35 ATR lower.
            # Cancel if not touched in the next 4 completed 15m bars.
            entry = candle['close'] - args.pullback_atr * current_atr
            fill_i = None
            for j in range(i + 1, min(i + 1 + args.pullback_bars, len(candles))):
                if candles[j]['low'] <= entry:
                    fill_i = j
                    break
            if fill_i is None:
                diagnostics['Unfilled limit order'] += 1
                continue
        stop = min(x['low'] for x in candles[i - 9:i + 1]) - 0.15 * current_atr
        risk = entry - stop
        if risk <= 0:
            diagnostics['Invalid stop'] += 1
            continue
        target = entry + 2 * risk
        if version == 'V4B':
            # Intrabar path is unknown on fill candle: if stop touched,
            # count it as a loss (including a simultaneous target hit).
            fill_bar = candles[fill_i]
            if fill_bar['low'] <= stop:
                status = 'STOP'
                net_r = -1 - 2 * entry * (args.fee_bps + args.slippage_bps) / 10000 / risk
                minutes = (fill_bar['ts'] - candle['ts']) / 60000
                exit_time = fill_bar['ts'] + 900000
            else:
                status, net_r, minutes, exit_time = evaluate(
                    candles, fill_i, entry, stop, target, args.fee_bps, args.slippage_bps)
        else:
            status, net_r, minutes, exit_time = evaluate(
                candles, i, entry, stop, target, args.fee_bps, args.slippage_bps)
        if status == 'INVALID':
            continue
        diagnostics['SIGNAL'] += 1
        active_until = exit_time if exit_time is not None else float('inf')
        trades.append({
            'version': version, 'symbol': symbol, 'side': 'LONG',
            'signal_time': datetime.fromtimestamp(signal_close / 1000, timezone.utc).isoformat(),
            'pattern': pattern, 'entry': entry, 'stop': stop, 'tp2': target,
            'status': status, 'net_r': net_r, 'duration_min': minutes,
            'exit_time': (datetime.fromtimestamp(exit_time / 1000, timezone.utc).isoformat()
                          if exit_time is not None else ''),
        })
    return trades


def rsi_series(closes, period=2):
    result = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains = [max(closes[k] - closes[k-1], 0) for k in range(1, period+1)]
    losses = [max(closes[k-1] - closes[k], 0) for k in range(1, period+1)]
    avg_gain, avg_loss = sum(gains)/period, sum(losses)/period
    for i in range(period, len(closes)):
        if i > period:
            change = closes[i] - closes[i-1]
            avg_gain = (avg_gain * (period-1) + max(change, 0))/period
            avg_loss = (avg_loss * (period-1) + max(-change, 0))/period
        result[i] = 100 if avg_loss == 0 else 100 - 100/(1 + avg_gain/avg_loss)
    return result


def connors_backtest(candles, version, symbol, args, diagnostics):
    closes = [c['close'] for c in candles]
    ema200 = ema_series(closes, 200)
    rsi2 = rsi_series(closes, 2)
    sma5 = [None] * len(closes)
    for i in range(4, len(closes)):
        sma5[i] = sum(closes[i-4:i+1])/5
    trades = []
    active_until = 0
    for i in range(220, len(candles)-1):
        signal = candles[i]
        signal_close = signal['ts'] + 900000
        if signal_close < active_until:
            continue
        if not (closes[i] > ema200[i] and rsi2[i] < args.connors_rsi):
            continue
        if version == 'CONNORS_TREND' and not ema200[i] > ema200[i-16]:
            continue
        # Signal is known only after bar i closes; assume fill at next bar OPEN.
        fill_i = i + 1
        entry = candles[fill_i]['open']
        current_atr = atr(candles, i)
        stop = entry - args.connors_stop_atr * current_atr
        risk = entry - stop
        if risk <= 0:
            continue
        target = entry + 2*risk  # reference 2R, not the Connors exit
        status, net_r, exit_time, duration = 'OPEN', None, '', None
        last = min(fill_i + args.connors_max_bars, len(candles))
        for j in range(fill_i, last):
            bar = candles[j]
            if bar['low'] <= stop:
                status, exit_price = 'STOP', stop
            elif j > fill_i and bar['close'] > sma5[j]:
                # Exit on close when SMA5 crossover is confirmed.
                status, exit_price = 'SMA_EXIT', bar['close']
            elif j == last - 1 and last == fill_i + args.connors_max_bars:
                status, exit_price = 'TIME_EXIT', bar['close']
            else:
                continue
            cost = 2 * entry * (args.fee_bps + args.slippage_bps) / 10000
            net_r = (exit_price - entry - cost) / risk
            exit_time = datetime.fromtimestamp((bar['ts'] + 900000)/1000, timezone.utc).isoformat()
            duration = (bar['ts'] - candles[fill_i]['ts'])/60000 + 15
            break
        active_until = (datetime.fromisoformat(exit_time).timestamp()*1000 if exit_time else float('inf'))
        diagnostics['SIGNAL'] += 1
        trades.append({'version': version, 'symbol': symbol, 'side': 'LONG',
            'signal_time': datetime.fromtimestamp(signal_close/1000, timezone.utc).isoformat(),
            'pattern': 'RSI2 mean reversion', 'entry': entry, 'stop': stop,
            'tp2': target, 'status': status, 'net_r': net_r,
            'duration_min': duration, 'exit_time': exit_time})
    return trades


def report(trades, version):
    selected = [trade for trade in trades if trade['version'] == version]
    closed = [trade for trade in selected if trade['net_r'] is not None and trade['status'] not in ('UNCLEAR', 'OPEN')]
    wins = sum(trade['net_r'] > 0 for trade in closed)
    net = sum(trade['net_r'] for trade in closed)
    # Portfolio-level R and drawdown require risk allocation and concurrent position limits.
    equity = peak = max_drawdown = 0.0
    for trade in sorted(closed, key=lambda t: t['exit_time']):
        equity += trade['net_r']
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    print(f'{version}: {len(selected)} signals | {len(closed)} closed | '
          f'win rate {100 * wins / len(closed):.1f}% | net {net:+.2f}R | '
          f'sequential trade drawdown {max_drawdown:.2f}R' if closed
          else f'{version}: {len(selected)} signals | no closed trades', flush=True)
    print(f'  OPEN={sum(t["status"] == "OPEN" for t in selected)} '
          f'UNCLEAR={sum(t["status"] == "UNCLEAR" for t in selected)}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--days', type=int, default=90)
    parser.add_argument('--markets', type=int, default=100)
    parser.add_argument('--fee-bps', type=float, default=5)
    parser.add_argument('--slippage-bps', type=float, default=2)
    parser.add_argument('--pullback-atr', type=float, default=0.35)
    parser.add_argument('--pullback-bars', type=int, default=4)
    parser.add_argument('--connors-rsi', type=float, default=5)
    parser.add_argument('--connors-stop-atr', type=float, default=1.5)
    parser.add_argument('--connors-max-bars', type=int, default=32)
    parser.add_argument('--pause', type=float, default=0.12)
    parser.add_argument('--output', default='backtest_trades.csv')
    parser.add_argument('--diagnostics', default='backtest_filters.csv')
    args = parser.parse_args()
    if args.days < 1 or not 1 <= args.markets <= 400 or args.pullback_bars < 1 or args.pullback_atr <= 0:
        parser.error('Require days >= 1, markets 1..400, pullback-bars >= 1, pullback-atr > 0')
    end = int(time.time() * 1000) - 3600000
    start = end - args.days * 86400000
    warmup = start - 7 * 86400000
    symbols = markets(args.markets)
    all_trades, filter_counts = [], []
    for number, symbol in enumerate(symbols, 1):
        print(f'{number}/{len(symbols)} {symbol}', flush=True)
        try:
            candles = history(symbol, '15m', warmup, end, args.pause)
            hourly = history(symbol, '1H', warmup, end, args.pause)
            if len(candles) < 60 or len(hourly) < 55:
                print('  Skipped: insufficient history', flush=True)
                continue
            for version in ('V4', 'CONNORS', 'CONNORS_TREND'):
                counts = Counter()
                trades = (backtest(candles, hourly, version, symbol, args, counts) if version == 'V4'
                          else connors_backtest(candles, version, symbol, args, counts))
                all_trades.extend(trade for trade in trades
                                  if datetime.fromisoformat(trade['signal_time']).timestamp() * 1000 >= start)
                filter_counts.append({'version': version, 'symbol': symbol, **counts})
        except Exception as error:
            print(f'  ERROR: {error}', flush=True)
    fields = ['version', 'symbol', 'side', 'signal_time', 'pattern', 'entry', 'stop',
              'tp2', 'status', 'net_r', 'duration_min', 'exit_time']
    with open(args.output, 'w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_trades)
    reasons = sorted({key for row in filter_counts for key in row if key not in ('version', 'symbol')})
    with open(args.diagnostics, 'w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=['version', 'symbol'] + reasons)
        writer.writeheader()
        writer.writerows(filter_counts)
    for version in ('V4', 'CONNORS', 'CONNORS_TREND'):
        report(all_trades, version)
    print('Results:', args.output, '| Filters:', args.diagnostics)
    print('WARNING: Connors uses next-open entry, ATR stop, SMA5/time exit; OHLC stop-first;  current top-volume symbols (survivorship bias), OHLC close-entry assumption, '
          'no funding, no shared portfolio sizing or capital limits; not live P&L.')


if __name__ == '__main__':
    main()
