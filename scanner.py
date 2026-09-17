import csv
import json
import math
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://www.okx.com"
JOURNAL = "signals.csv"

# Anzahl der liquidesten USDT-Perpetuals
MAX_MARKETS = 100

FIELDS = [
    "time", "symbol", "side", "timeframe", "setup_type",
    "entry", "stop", "tp1", "tp2", "tp3", "planned_rr",
    "trigger", "status", "last_checked"
]


def api(path, params=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                obj = json.loads(r.read().decode())
            if obj.get("code") != "0":
                raise RuntimeError(obj.get("msg", "OKX API error"))
            return obj["data"]
        except Exception:
            if attempt == 2:
                raise
            time.sleep(1.5)


def get_markets():
    instruments = api(
        "/api/v5/public/instruments",
        {"instType": "SWAP"}
    )

    tickers = api(
        "/api/v5/market/tickers",
        {"instType": "SWAP"}
    )

    allowed = set()

    for x in instruments:
        inst = x.get("instId", "")
        if (
            x.get("state") == "live"
            and inst.endswith("-USDT-SWAP")
            and x.get("ctType") == "linear"
        ):
            allowed.add(inst)

    ranked = []

    for t in tickers:
        inst = t.get("instId", "")
        if inst not in allowed:
            continue

        try:
            volume = float(t.get("volCcy24h") or 0)
            last = float(t.get("last") or 0)
        except ValueError:
            continue

        if last > 0 and volume > 0:
            ranked.append((volume, inst))

    ranked.sort(reverse=True)

    return [x[1] for x in ranked[:MAX_MARKETS]]


def candles(symbol, bar, limit=120):
    data = api(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": bar,
            "limit": str(limit)
        }
    )

    out = []

    for x in data:
        # OKX: ts,o,h,l,c,vol,volCcy,volCcyQuote,confirm
        if len(x) < 9:
            continue

        # Nur abgeschlossene Kerzen
        if str(x[8]) != "1":
            continue

        out.append({
            "ts": int(x[0]),
            "o": float(x[1]),
            "h": float(x[2]),
            "l": float(x[3]),
            "c": float(x[4]),
            "v": float(x[5])
        })

    out.sort(key=lambda x: x["ts"])
    return out


def ema(values, period):
    if not values:
        return 0

    k = 2 / (period + 1)
    result = values[0]

    for value in values[1:]:
        result = value * k + result * (1 - k)

    return result


def atr(cs, period=14):
    if len(cs) < period + 1:
        return 0

    trs = []

    for i in range(1, len(cs)):
        high = cs[i]["h"]
        low = cs[i]["l"]
        prev_close = cs[i - 1]["c"]

        trs.append(
            max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close)
            )
        )

    return sum(trs[-period:]) / period


def avg_volume(cs, n=20):
    if len(cs) < n + 1:
        return 0

    vals = [x["v"] for x in cs[-n - 1:-1]]
    return sum(vals) / len(vals)


def candle_patterns(cs):
    if len(cs) < 3:
        return []

    prev = cs[-2]
    c = cs[-1]

    body = abs(c["c"] - c["o"])
    rng = max(c["h"] - c["l"], 1e-12)

    upper = c["h"] - max(c["o"], c["c"])
    lower = min(c["o"], c["c"]) - c["l"]

    patterns = []

    # Engulfing
    if (
        prev["c"] < prev["o"]
        and c["c"] > c["o"]
        and c["o"] <= prev["c"]
        and c["c"] >= prev["o"]
    ):
        patterns.append("Bullish Engulfing")

    if (
        prev["c"] > prev["o"]
        and c["c"] < c["o"]
        and c["o"] >= prev["c"]
        and c["c"] <= prev["o"]
    ):
        patterns.append("Bearish Engulfing")

    # Hammer / bullish rejection
    if lower >= body * 1.8 and lower > upper * 1.5:
        patterns.append("Bullish Rejection")

    # Shooting star / bearish rejection
    if upper >= body * 1.8 and upper > lower * 1.5:
        patterns.append("Bearish Rejection")

    # Outside bar
    if c["h"] > prev["h"] and c["l"] < prev["l"]:
        if c["c"] > c["o"]:
            patterns.append("Bullish Outside Bar")
        elif c["c"] < c["o"]:
            patterns.append("Bearish Outside Bar")

    # starke Impulskerze
    if body / rng >= 0.70:
        if c["c"] > c["o"]:
            patterns.append("Bullish Displacement")
        elif c["c"] < c["o"]:
            patterns.append("Bearish Displacement")

    return patterns


def structure_bias(cs):
    if len(cs) < 55:
        return "neutral"

    closes = [x["c"] for x in cs]

    e20 = ema(closes[-50:], 20)
    e50 = ema(closes[-70:], 50)

    recent_high = max(x["h"] for x in cs[-10:-2])
    recent_low = min(x["l"] for x in cs[-10:-2])

    c = cs[-1]["c"]

    if c > e20 and e20 > e50:
        return "bullish"

    if c < e20 and e20 < e50:
        return "bearish"

    if c > recent_high:
        return "bullish"

    if c < recent_low:
        return "bearish"

    return "neutral"


def setup_30m(cs):
    if len(cs) < 30:
        return []

    c = cs[-1]
    prev = cs[-2]

    lookback = cs[-22:-2]

    resistance = max(x["h"] for x in lookback)
    support = min(x["l"] for x in lookback)

    rng = max(resistance - support, 1e-12)

    setups = []

    # Breakout
    if prev["c"] <= resistance and c["c"] > resistance:
        setups.append(("long", "30m Breakout"))

    if prev["c"] >= support and c["c"] < support:
        setups.append(("short", "30m Breakdown"))

    # Liquidity sweep + reclaim
    if c["l"] < support and c["c"] > support:
        setups.append(("long", "Liquidity Sweep/Reclaim"))

    if c["h"] > resistance and c["c"] < resistance:
        setups.append(("short", "Liquidity Sweep/Reclaim"))

    # Rejection an Rand der Range
    pos = (c["c"] - support) / rng

    patterns = candle_patterns(cs)

    if pos < 0.25 and any(
        "Bullish" in p for p in patterns
    ):
        setups.append(("long", "Support Rejection"))

    if pos > 0.75 and any(
        "Bearish" in p for p in patterns
    ):
        setups.append(("short", "Resistance Rejection"))

    return setups


def trigger_15m(cs, side):
    if len(cs) < 30:
        return None

    c = cs[-1]
    prev = cs[-2]

    patterns = candle_patterns(cs)

    av = avg_volume(cs)
    volume_ok = av == 0 or c["v"] >= av * 0.80

    highs = [x["h"] for x in cs[-8:-1]]
    lows = [x["l"] for x in cs[-8:-1]]

    local_high = max(highs)
    local_low = min(lows)

    if side == "long":
        bullish_pattern = next(
            (p for p in patterns if "Bullish" in p),
            None
        )

        structure_break = (
            c["c"] > local_high
            or (c["c"] > prev["h"] and c["c"] > c["o"])
        )

        sweep = (
            c["l"] < local_low
            and c["c"] > local_low
            and c["c"] > c["o"]
        )

        if volume_ok and (bullish_pattern or structure_break or sweep):
            parts = []

            if bullish_pattern:
                parts.append(bullish_pattern)
            if structure_break:
                parts.append("15m Structure Break")
            if sweep:
                parts.append("15m Sweep/Reclaim")
            if volume_ok:
                parts.append("Volume OK")

            return " + ".join(parts)

    if side == "short":
        bearish_pattern = next(
            (p for p in patterns if "Bearish" in p),
            None
        )

        structure_break = (
            c["c"] < local_low
            or (c["c"] < prev["l"] and c["c"] < c["o"])
        )

        sweep = (
            c["h"] > local_high
            and c["c"] < local_high
            and c["c"] < c["o"]
        )

        if volume_ok and (bearish_pattern or structure_break or sweep):
            parts = []

            if bearish_pattern:
                parts.append(bearish_pattern)
            if structure_break:
                parts.append("15m Structure Break")
            if sweep:
                parts.append("15m Sweep/Reclaim")
            if volume_ok:
                parts.append("Volume OK")

            return " + ".join(parts)

    return None


def make_trade(symbol, side, setup_type, trigger, cs15):
    c = cs15[-1]
    a = atr(cs15)

    if a <= 0:
        return None

    entry = c["c"]

    if side == "long":
        recent_low = min(x["l"] for x in cs15[-6:])
        stop = min(recent_low, entry - a * 0.8)
        risk = entry - stop

        if risk <= 0:
            return None

        tp1 = entry + risk * 1.25
        tp2 = entry + risk * 2.0
        tp3 = entry + risk * 3.0

    else:
        recent_high = max(x["h"] for x in cs15[-6:])
        stop = max(recent_high, entry + a * 0.8)
        risk = stop - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * 1.25
        tp2 = entry - risk * 2.0
        tp3 = entry - risk * 3.0

    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "side": side.upper(),
        "timeframe": "15m",
        "setup_type": setup_type,
        "entry": entry,
        "stop": stop,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        "planned_rr": "2.0",
        "trigger": trigger,
        "status": "OPEN",
        "last_checked": datetime.now(timezone.utc).isoformat()
    }


def load_journal():
    if not os.path.exists(JOURNAL):
        return []

    with open(JOURNAL, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def save_journal(rows):
    with open(JOURNAL, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()

        for row in rows:
            w.writerow({k: row.get(k, "") for k in FIELDS})


def update_open_trades(rows):
    changed = []

    for row in rows:
        if row.get("status") not in (
            "OPEN", "TP1", "TP2"
        ):
            continue

        symbol = row["symbol"]

        try:
            cs = candles(symbol, "15m", 100)
        except Exception:
            continue

        try:
            signal_time = datetime.fromisoformat(
                row["time"].replace("Z", "+00:00")
            ).timestamp() * 1000

            stop = float(row["stop"])
            tp1 = float(row["tp1"])
            tp2 = float(row["tp2"])
            tp3 = float(row["tp3"])
        except Exception:
            continue

        old = row["status"]
        status = old

        for c in cs:
            if c["ts"] <= signal_time:
                continue

            if row["side"] == "LONG":
                hit_stop = c["l"] <= stop
                hit1 = c["h"] >= tp1
                hit2 = c["h"] >= tp2
                hit3 = c["h"] >= tp3
            else:
                hit_stop = c["h"] >= stop
                hit1 = c["l"] <= tp1
                hit2 = c["l"] <= tp2
                hit3 = c["l"] <= tp3

            # Stop und Target in derselben Kerze:
            # Reihenfolge nicht objektiv feststellbar.
            if hit_stop and (hit1 or hit2 or hit3):
                status = "UNCLEAR"
                break

            if hit_stop:
                status = "STOP"
                break

            if hit3:
                status = "TP3"
                break

            if hit2:
                status = "TP2"

            elif hit1 and status == "OPEN":
                status = "TP1"

        row["status"] = status
        row["last_checked"] = datetime.now(timezone.utc).isoformat()

        if status != old:
            changed.append(
                f"{symbol} {row['side']}: {old} -> {status}"
            )

        time.sleep(0.06)

    return changed


def duplicate(rows, symbol, side):
    for row in reversed(rows[-300:]):
        if (
            row.get("symbol") == symbol
            and row.get("side") == side.upper()
            and row.get("status") in ("OPEN", "TP1", "TP2")
        ):
            return True
    return False


def main():
    rows = load_journal()

    changes = update_open_trades(rows)

    markets = get_markets()

    print(f"OKX markets selected: {len(markets)}")

    scanned = 0
    errors = 0
    signals = []

    for i, symbol in enumerate(markets, 1):
        try:
            c1h = candles(symbol, "1H", 100)
            time.sleep(0.06)

            c30 = candles(symbol, "30m", 100)
            time.sleep(0.06)

            c15 = candles(symbol, "15m", 120)
            time.sleep(0.06)

            if min(len(c1h), len(c30), len(c15)) < 30:
                continue

            scanned += 1

            bias = structure_bias(c1h)
            setups = setup_30m(c30)

            for side, setup_type in setups:

                # 1h ist Kontext, kein extrem harter Filter.
                # Nur klar gegensätzliche Trades werden vermieden.
                if side == "long" and bias == "bearish":
                    continue

                if side == "short" and bias == "bullish":
                    continue

                trigger = trigger_15m(c15, side)

                if not trigger:
                    continue

                if duplicate(rows, symbol, side):
                    continue

                trade = make_trade(
                    symbol,
                    side,
                    setup_type,
                    trigger,
                    c15
                )

                if trade:
                    rows.append(trade)
                    signals.append(trade)

        except Exception as e:
            errors += 1
            print(f"ERROR {symbol}: {e}")

        if i % 10 == 0:
            print(
                f"Progress: {i}/{len(markets)} | "
                f"scanned={scanned} errors={errors}"
            )

    save_journal(rows)

    print("")
    print("========== SCAN RESULT ==========")
    print(f"Successfully scanned: {scanned}")
    print(f"Errors: {errors}")
    print(f"Confirmed setups: {len(signals)}")

    for s in signals:
        print("")
        print(
            f"{s['symbol']} {s['side']} | "
            f"{s['setup_type']}"
        )
        print(f"Trigger: {s['trigger']}")
        print(f"Entry: {s['entry']}")
        print(f"Stop: {s['stop']}")
        print(f"TP1: {s['tp1']}")
        print(f"TP2: {s['tp2']}")
        print(f"TP3: {s['tp3']}")

    if changes:
        print("")
        print("Journal updates:")
        for x in changes:
            print(x)

    print("=================================")


if __name__ == "__main__":
    main()
