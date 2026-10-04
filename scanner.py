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
TEST_LIMIT = 100
V2_STRATEGY = "V2"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FIELDS = [
    "time", "strategy", "symbol", "side", "timeframe", "setup_type",
    "entry", "stop", "tp1", "tp2", "tp3", "planned_rr",
    "trigger", "status", "last_checked"
]
TERMINAL = {"STOP", "TP3", "UNCLEAR", "TP1_THEN_STOP", "TP2_THEN_STOP"}


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
            headers={"User-Agent": "CryptoScanner/V2"}
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
        BASE + path,
        headers={"User-Agent": "Mozilla/5.0 CryptoScanner/V2"}
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
        symbol = x.get("instId", "")
        base = symbol.split("-")[0] if symbol else ""
        if x.get("state") == "live" and symbol.endswith("-USDT-SWAP") and base not in excluded:
            out.append((symbol, vols.get(symbol, 0)))
    out.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in out[:MAX_MARKETS]]


def candles(symbol, bar, limit=120):
    raw = api("/api/v5/market/candles", {"instId": symbol, "bar": bar, "limit": str(limit)})
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


def rsi(values, period=14):
    if len(values) < period + 2:
        return None
    gains, losses = [], []
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def atr(d, period=14):
    if len(d) < 2:
        return 0
    tr = []
    for i in range(1, len(d)):
        tr.append(max(
            d[i]["high"] - d[i]["low"],
            abs(d[i]["high"] - d[i - 1]["close"]),
            abs(d[i]["low"] - d[i - 1]["close"])
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
    return lower_wick(c) >= max(body(c) * 1.5, rng(c) * 0.30) and c["close"] >= c["low"] + rng(c) * 0.60


def bear_reject(c):
    return upper_wick(c) >= max(body(c) * 1.5, rng(c) * 0.30) and c["close"] <= c["low"] + rng(c) * 0.40


def bull_outside(p, c):
    return c["high"] > p["high"] and c["low"] < p["low"] and bull(c)


def bear_outside(p, c):
    return c["high"] > p["high"] and c["low"] < p["low"] and bear(c)


def displacement(d, side):
    if len(d) < 10:
        return False
    avgb = sum(body(x) for x in d[-10:-1]) / 9
    if avgb <= 0:
        return False
    if side == "LONG":
        return bull(d[-1]) and body(d[-1]) >= avgb * 1.5
    return bear(d[-1]) and body(d[-1]) >= avgb * 1.5


def trend_1h(d):
    if len(d) < 60:
        return "NEUTRAL"
    closes = [x["close"] for x in d]
    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    old, new = d[-24:-12], d[-12:]

    higher = max(x["high"] for x in new) > max(x["high"] for x in old) and min(x["low"] for x in new) > min(x["low"] for x in old)
    lower = max(x["high"] for x in new) < max(x["high"] for x in old) and min(x["low"] for x in new) < min(x["low"] for x in old)

    if closes[-1] > e20[-1] > e50[-1] and e20[-1] > e20[-5] and higher:
        return "LONG"
    if closes[-1] < e20[-1] < e50[-1] and e20[-1] < e20[-5] and lower:
        return "SHORT"
    return "NEUTRAL"


def setup_30m(d, side):
    if len(d) < 30:
        return None
    c, p = d[-1], d[-2]
    hist = d[-22:-2]
    res = max(x["high"] for x in hist)
    sup = min(x["low"] for x in hist)
    a = atr(d)
    if a <= 0:
        return None
    tol = a * 0.35
    av = avgvol(d[:-1])
    vr = c["volume"] / av if av else 0

    if side == "LONG":
        retest = (
            p["close"] > res and
            res - tol <= c["low"] <= res + tol and
            c["close"] > res and bull(c) and
            (bull_reject(c) or bull_engulf(p, c))
        )
        breakout = c["close"] > res and bull(c) and vr >= 0.90
        if retest:
            return "30m Breakout + Retest"
        if breakout:
            return "30m bestÃ¤tigter Breakout"

    if side == "SHORT":
        retest = (
            p["close"] < sup and
            sup - tol <= c["high"] <= sup + tol and
            c["close"] < sup and bear(c) and
            (bear_reject(c) or bear_engulf(p, c))
        )
        breakdown = c["close"] < sup and bear(c) and vr >= 1.00
        if retest:
            return "30m Breakdown + Retest"
        if breakdown:
            return "30m bestÃ¤tigter Breakdown"

    return None


def trigger_15m(d, side):
    if len(d) < 30:
        return None
    c, p = d[-1], d[-2]
    recent = d[-8:-1]
    r = rsi([x["close"] for x in d], 14)
    if r is None:
        return None
    av = avgvol(d[:-1])
    vr = c["volume"] / av if av else 0

    if side == "LONG":
        pattern = bull_engulf(p, c) or bull_reject(c) or bull_outside(p, c) or displacement(d, "LONG")
        brk = c["close"] > max(x["high"] for x in recent)
        if pattern and brk and 50 <= r <= 68 and vr >= 0.85:
            return f"Bullische 15m-BestÃ¤tigung + Strukturbruch + RSI {r:.1f} + Volumen"
    else:
        pattern = bear_engulf(p, c) or bear_reject(c) or bear_outside(p, c) or displacement(d, "SHORT")
        brk = c["close"] < min(x["low"] for x in recent)
        if pattern and brk and 32 <= r <= 50 and vr >= 1.00:
            return f"BÃ¤rische 15m-BestÃ¤tigung + Strukturbruch + RSI {r:.1f} + Volumen"
    return None


def trade(symbol, side, setup, trigger, d):
    entry = d[-1]["close"]
    a = atr(d)
    if a <= 0:
        return None
    recent = d[-8:]

    if side == "LONG":
        stop = min(min(x["low"] for x in recent) - a * 0.10, entry - a * 0.80)
        risk = entry - stop
        if risk <= 0:
            return None
        t1, t2, t3 = entry + risk * 1.25, entry + risk * 2, entry + risk * 3
    else:
        stop = max(max(x["high"] for x in recent) + a * 0.10, entry + a * 0.80)
        risk = stop - entry
        if risk <= 0:
            return None
        t1, t2, t3 = entry - risk * 1.25, entry - risk * 2, entry - risk * 3

    t = now()
    return {
        "time": t, "strategy": V2_STRATEGY, "symbol": symbol, "side": side,
        "timeframe": "1h/30m/15m", "setup_type": setup,
        "entry": num(entry), "stop": num(stop), "tp1": num(t1),
        "tp2": num(t2), "tp3": num(t3), "planned_rr": "3R TP3",
        "trigger": trigger, "status": "OPEN", "last_checked": t
    }


def load():
    if not os.path.exists(JOURNAL):
        return []
    with open(JOURNAL, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        if not r.get("strategy"):
            r["strategy"] = "CURRENT"
    return rows


def save(rows):
    with open(JOURNAL, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def duplicate(rows, symbol, side):
    return any(
        r.get("strategy") == V2_STRATEGY and
        r.get("symbol") == symbol and
        r.get("side") == side and
        r.get("status") in {"OPEN", "TP1", "TP2"}
        for r in rows
    )


def v2_rows(rows):
    return [r for r in rows if r.get("strategy") == V2_STRATEGY]


def test_stats(rows):
    test = v2_rows(rows)
    done = [r for r in test if r.get("status") in TERMINAL]
    tp3 = [r for r in done if r.get("status") == "TP3"]
    rate = len(tp3) / len(done) * 100 if done else 0
    return {
        "total": len(test),
        "completed": len(done),
        "open": len(test) - len(done),
        "tp3": len(tp3),
        "stop": sum(r.get("status") == "STOP" for r in done),
        "partial": sum(r.get("status") in {"TP1_THEN_STOP", "TP2_THEN_STOP"} for r in done),
        "unclear": sum(r.get("status") == "UNCLEAR" for r in done),
        "rate": rate
    }


def strategy_name(name):
    if name == V2_STRATEGY:
        return "V2 â QualitÃ¤tsstrategie"
    if name == "CREAMER_CRYPTO":
        return "Creamer-Strategie"
    return "Bisherige Strategie"


def notify_trade(t):
    direction = "ð¢ LONG" if t["side"] == "LONG" else "ð´ SHORT"
    telegram(
        f"ð¨ NEUES V2-SIGNAL\n\n"
        f"Paar: {t['symbol']}\n"
        f"Richtung: {direction}\n"
        f"Zeitrahmen: {t['timeframe']}\n"
        f"Setup: {t['setup_type']}\n"
        f"BestÃ¤tigung: {t['trigger']}\n\n"
        f"Einstieg: {t['entry']}\n"
        f"Stop-Loss: {t['stop']}\n"
        f"Ziel 1: {t['tp1']}\n"
        f"Ziel 2: {t['tp2']}\n"
        f"Ziel 3: {t['tp3']}\n"
        f"TP3-Ziel: 3R\n\n"
        f"V2-Test: maximal {TEST_LIMIT} Signale"
    )


def update_journal(rows):
    changes = []
    for r in rows:
        if r.get("status") in TERMINAL or r.get("status") not in {"OPEN", "TP1", "TP2"}:
            continue
        try:
            start = int(datetime.fromisoformat(r["time"].replace("Z", "+00:00")).timestamp() * 1000)
            d = [c for c in candles(r["symbol"], "15m", 100) if c["ts"] > start]
            old = r["status"]
            progress = {"OPEN": 0, "TP1": 1, "TP2": 2}.get(old, 0)
            stop, tp1, tp2, tp3 = map(flt, [r["stop"], r["tp1"], r["tp2"], r["tp3"]])
            status = old

            for c in d:
                if r["side"] == "LONG":
                    sh = c["low"] <= stop
                    hits = [c["high"] >= tp1, c["high"] >= tp2, c["high"] >= tp3]
                else:
                    sh = c["high"] >= stop
                    hits = [c["low"] <= tp1, c["low"] <= tp2, c["low"] <= tp3]

                high = 3 if hits[2] else 2 if hits[1] else 1 if hits[0] else 0

                if sh and high > progress:
                    status = "UNCLEAR"
                    break
                if high > progress:
                    progress = high
                    status = {1: "TP1", 2: "TP2", 3: "TP3"}[progress]
                    if progress == 3:
                        break
                if sh:
                    status = "STOP" if progress == 0 else f"TP{progress}_THEN_STOP"
                    break

            r["status"] = status
            r["last_checked"] = now()

            if status != old:
                msg = (
                    f"ð SIGNAL-UPDATE â {strategy_name(r.get('strategy', 'CURRENT'))}\n\n"
                    f"Paar: {r['symbol']}\n"
                    f"Richtung: {r['side']}\n"
                    f"Status: {old} â {status}"
                )
                changes.append(msg)
                telegram(msg)
        except Exception as e:
            print("Journal error", r.get("symbol"), e)
        time.sleep(PAUSE)
    return changes


def scan(symbol, rows):
    h1 = candles(symbol, "1H", 100)
    time.sleep(PAUSE)
    m30 = candles(symbol, "30m", 120)
    time.sleep(PAUSE)
    m15 = candles(symbol, "15m", 120)

    side = trend_1h(h1)
    if side not in {"LONG", "SHORT"}:
        return None

    setup = setup_30m(m30, side)
    if not setup:
        return None

    trigger = trigger_15m(m15, side)
    if not trigger or duplicate(rows, symbol, side):
        return None

    return trade(symbol, side, setup, trigger, m15)


def main():
    print("Crypto Scanner V2 gestartet")
    rows = load()

    # Bestehende alte Trades bleiben erhalten und werden weiter aktualisiert.
    changes = update_journal(rows)

    try:
        ms = markets()
    except Exception as e:
        telegram(f"â Scanner-Fehler: Market-Liste nicht geladen\n{e}")
        save(rows)
        raise

    scanned = errors = new_v2 = 0

    if len(v2_rows(rows)) >= TEST_LIMIT:
        print("V2-Testlimit erreicht: keine neuen Signale.")
    else:
        for i, symbol in enumerate(ms, 1):
            if len(v2_rows(rows)) >= TEST_LIMIT:
                break
            try:
                found = scan(symbol, rows)
                scanned += 1
                if found:
                    rows.append(found)
                    new_v2 += 1
                    print(f"NEW [V2]: {symbol} {found['side']}")
                    notify_trade(found)
            except Exception as e:
                errors += 1
                print("ERROR", symbol, e)

            if i % 10 == 0:
                print(f"Progress: {i}/{len(ms)}")
            time.sleep(PAUSE)

    save(rows)
    s = test_stats(rows)

    print("Successfully scanned:", scanned)
    print("Errors:", errors)
    print("Neue V2-Signale:", new_v2)
    print("V2 total:", s["total"])
    print("V2 completed:", s["completed"])
    print("V2 TP3:", s["tp3"])
    print("V2 TP3 rate:", f'{s["rate"]:.1f}%')

    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        telegram(
            "â CRYPTO-SCANNER V2 AKTIV\n\n"
            f"MÃ¤rkte geprÃ¼ft: {scanned}/{len(ms)}\n"
            f"Fehler: {errors}\n"
            f"Neue V2-Signale: {new_v2}\n"
            f"Journal-Aktualisierungen: {len(changes)}\n\n"
            f"V2-Test: {s['total']}/{TEST_LIMIT} Signale\n"
            f"Abgeschlossen: {s['completed']}\n"
            f"Noch offen: {s['open']}\n"
            f"TP3 komplett: {s['tp3']}\n"
            f"Direkt Stop: {s['stop']}\n"
            f"Teilziel â Stop: {s['partial']}\n"
            f"Unklar: {s['unclear']}\n"
            f"TP3-Quote: {s['rate']:.1f}%"
        )


if __name__ == "__main__":
    main()