import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE = "https://www.okx.com"
JOURNAL = "signals.csv"
STRATEGY = "V5_MEAN_REVERSION"

MAX_MARKETS = 400
MAX_WORKERS = 12
TEST_LIMIT = 100
API_TIMEOUT = 6
API_RETRIES = 1

RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 70
RSI_LONG_RECOVERY = 40
RSI_SHORT_RECOVERY = 65

ATR_PERIOD = 14
ATR_MULTIPLIER = 2.0
RR = 2.0

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FIELDS = [
    "time", "strategy", "symbol", "side", "timeframe", "setup_type",
    "entry", "stop", "tp1", "tp2", "tp3", "planned_rr",
    "trigger", "status", "last_checked"
]

MEME_BASES = {
    "DOGE","SHIB","PEPE","BONK","FLOKI","WIF","BOME","MEME","TURBO","NEIRO",
    "BRETT","MOG","POPCAT","MEW","PONKE","SLERF","BABYDOGE","DOGS","CAT",
    "HIPPO","PNUT","GOAT","ACT","MOODENG","TRUMP","MELANIA"
}
STABLE_BASES = {
    "USDT","USDC","DAI","FDUSD","TUSD","USDE","PYUSD","USDS","BUSD","USD0","FRAX"
}
TRADFI_BASES = {
    "AAPL","AMD","AMZN","APP","COIN","CRWV","DKNG","GOOGL","H100","IBM","INTC",
    "INTW","IREN","META","MRNA","MSFT","MSTU","MU","NKE","NVDA","ONDS","ORCL",
    "PLTR","SOFTBANK","SPCX","TSLA","TSLL","TSM","VRT","WDC","XAG","XAU","AEHR","GPRO"
}

def now():
    return datetime.now(timezone.utc).isoformat()

def num(v):
    return format(float(v), ".12g")

def flt(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def telegram(text):
    if not TOKEN or not CHAT_ID:
        print("Telegram Secrets fehlen")
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
            headers={"User-Agent": "CryptoScanner/V5"}
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            result = json.loads(response.read().decode())
        if not result.get("ok"):
            raise RuntimeError(result)
        print("Telegram: neues V5 Setup gesendet")
        return True
    except Exception as e:
        print("Telegram Fehler:", e)
        return False

def api(path, params=None):
    if params:
        path += "?" + urllib.parse.urlencode(params)
    last_error = None
    for attempt in range(API_RETRIES + 1):
        try:
            req = urllib.request.Request(
                BASE + path,
                headers={"User-Agent": "Mozilla/5.0 CryptoScanner/V5"}
            )
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as response:
                result = json.loads(response.read().decode())
            if result.get("code") != "0":
                raise RuntimeError(result)
            return result.get("data", [])
        except Exception as e:
            last_error = e
            if attempt < API_RETRIES:
                time.sleep(0.35)
    raise last_error

def markets():
    instruments = api("/api/v5/public/instruments", {"instType": "SWAP"})
    tickers = api("/api/v5/market/tickers", {"instType": "SWAP"})
    volumes = {t.get("instId", ""): flt(t.get("volCcy24h")) for t in tickers}
    selected = []
    for inst in instruments:
        symbol = inst.get("instId", "")
        if not symbol.endswith("-USDT-SWAP") or inst.get("state") != "live":
            continue
        base = symbol.split("-")[0].upper()
        if base in STABLE_BASES or base in MEME_BASES or base in TRADFI_BASES:
            continue
        volume = volumes.get(symbol, 0)
        if volume > 0:
            selected.append((symbol, volume))
    selected.sort(key=lambda x: x[1], reverse=True)
    return [x[0] for x in selected[:MAX_MARKETS]]

def candles(symbol, bar="15m", limit=120):
    raw = api("/api/v5/market/candles", {
        "instId": symbol, "bar": bar, "limit": str(limit)
    })
    out = []
    for c in reversed(raw):
        if len(c) > 8 and c[8] != "1":
            continue
        out.append({
            "ts": int(c[0]), "open": float(c[1]), "high": float(c[2]),
            "low": float(c[3]), "close": float(c[4]), "volume": float(c[5])
        })
    return out

def rsi_series(closes, period=14):
    if len(closes) < period + 2:
        return []
    values = [None] * len(closes)
    gains, losses = [], []
    for i in range(1, period + 1):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    ag, al = sum(gains) / period, sum(losses) / period
    values[period] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(period + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (period - 1) + max(d, 0)) / period
        al = (al * (period - 1) + max(-d, 0)) / period
        values[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return values

def atr(data, period=14):
    if len(data) < period + 1:
        return 0
    trs = []
    for i in range(1, len(data)):
        c, p = data[i], data[i - 1]
        trs.append(max(
            c["high"] - c["low"],
            abs(c["high"] - p["close"]),
            abs(c["low"] - p["close"])
        ))
    vals = trs[-period:]
    return sum(vals) / len(vals) if vals else 0

def bullish(c):
    return c["close"] > c["open"]

def bearish(c):
    return c["close"] < c["open"]

def body(c):
    return abs(c["close"] - c["open"])

def candle_range(c):
    return max(c["high"] - c["low"], 1e-12)

def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])

def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]

def bullish_fib_hammer(c):
    if not bullish(c):
        return False
    r = candle_range(c)
    fib382 = c["high"] - 0.382 * r
    return (
        min(c["open"], c["close"]) >= fib382
        and lower_wick(c) >= max(2 * body(c), r * 0.40)
        and upper_wick(c) <= r * 0.25
    )

def bearish_fib_pinbar(c):
    if not bearish(c):
        return False
    r = candle_range(c)
    fib382 = c["low"] + 0.382 * r
    return (
        max(c["open"], c["close"]) <= fib382
        and upper_wick(c) >= max(2 * body(c), r * 0.40)
        and lower_wick(c) <= r * 0.25
    )

def bullish_engulfing(p, c):
    return (
        bearish(p) and bullish(c)
        and c["open"] <= p["close"]
        and c["close"] >= p["open"]
    )

def bearish_engulfing(p, c):
    return (
        bullish(p) and bearish(c)
        and c["open"] >= p["close"]
        and c["close"] <= p["open"]
    )

def rsi_recovery(rsis, side, lookback=10):
    valid = [x for x in rsis if x is not None]
    if len(valid) < lookback + 1:
        return None
    current = valid[-1]
    hist = valid[-(lookback + 1):-1]
    if side == "LONG":
        extreme = min(hist)
        if extreme <= RSI_OVERSOLD and current > RSI_LONG_RECOVERY:
            return {"extreme": extreme, "current": current}
    else:
        extreme = max(hist)
        if extreme >= RSI_OVERBOUGHT and current < RSI_SHORT_RECOVERY:
            return {"extreme": extreme, "current": current}
    return None

def recent_reversal(data, side):
    if len(data) < 5:
        return None
    for i in range(len(data) - 3, len(data)):
        c, p = data[i], data[i - 1]
        if side == "LONG":
            if not bullish(c):
                continue
            if bullish_engulfing(p, c):
                return {"name": "Bullish Engulfing", "index": i}
            if bullish_fib_hammer(c):
                return {"name": "Bullish Fib-0.382 Hammer", "index": i}
        else:
            if not bearish(c):
                continue
            if bearish_engulfing(p, c):
                return {"name": "Bearish Engulfing", "index": i}
            if bearish_fib_pinbar(c):
                return {"name": "Bearish Fib-0.382 Pinbar", "index": i}
    return None

def structure_break_after(data, side, reversal_index):
    if len(data) < 5:
        return None
    for i in range(reversal_index + 1, len(data)):
        if i < 3:
            continue
        c = data[i]
        prev = data[i - 3:i]
        if side == "LONG":
            if bullish(c) and c["close"] > max(x["high"] for x in prev):
                return i
        else:
            if bearish(c) and c["close"] < min(x["low"] for x in prev):
                return i
    return None

def volume_confirmation(data):
    if len(data) < 22:
        return False
    prev = data[-21:-1]
    avg = sum(x["volume"] for x in prev) / len(prev)
    return avg > 0 and data[-1]["volume"] >= avg * 1.20

def fvg_confirmation(data, side, lookback=16):
    if len(data) < 5:
        return False
    current = data[-1]
    for i in range(max(2, len(data) - lookback), len(data) - 1):
        first, third = data[i - 2], data[i]
        if side == "LONG" and third["low"] > first["high"]:
            if (
                current["low"] <= third["low"]
                and current["close"] >= first["high"]
                and bullish(current)
            ):
                return True
        if side == "SHORT" and third["high"] < first["low"]:
            if (
                current["high"] >= third["high"]
                and current["close"] <= first["low"]
                and bearish(current)
            ):
                return True
    return False

def triangle_confirmation(data, side, lookback=20):
    if len(data) < lookback + 1:
        return False
    hist, c = data[-(lookback + 1):-1], data[-1]
    half = len(hist) // 2
    first, second = hist[:half], hist[half:]
    if not first or not second:
        return False

    if side == "LONG":
        r1 = max(x["high"] for x in first)
        r2 = max(x["high"] for x in second)
        tol = max(r1, r2) * 0.006
        flat_resistance = abs(r2 - r1) <= tol
        rising_lows = min(x["low"] for x in second) > min(x["low"] for x in first)
        breakout = bullish(c) and c["close"] > max(x["high"] for x in hist)
        return flat_resistance and rising_lows and breakout

    s1 = min(x["low"] for x in first)
    s2 = min(x["low"] for x in second)
    tol = max(abs(s1), abs(s2)) * 0.006
    flat_support = abs(s2 - s1) <= tol
    falling_highs = max(x["high"] for x in second) < max(x["high"] for x in first)
    breakdown = bearish(c) and c["close"] < min(x["low"] for x in hist)
    return flat_support and falling_highs and breakdown

def quality_confirmations(data, side):
    extras = []
    if volume_confirmation(data):
        extras.append("Volumen bestätigt")
    if fvg_confirmation(data, side):
        extras.append("Bullish FVG bestätigt" if side == "LONG" else "Bearish FVG bestätigt")
    if triangle_confirmation(data, side):
        extras.append(
            "Ascending Triangle Breakout bestätigt"
            if side == "LONG"
            else "Descending Triangle Breakdown bestätigt"
        )
    return extras

def find_setup(data):
    if len(data) < 50:
        return None
    rsis = rsi_series([x["close"] for x in data], RSI_PERIOD)
    if not rsis:
        return None

    for side in ("LONG", "SHORT"):
        recovery = rsi_recovery(rsis, side)
        if not recovery:
            continue
        reversal = recent_reversal(data, side)
        if not reversal:
            continue

        break_index = structure_break_after(data, side, reversal["index"])
        if break_index is None:
            continue

        extras = quality_confirmations(data, side)
        reasons = [
            f"RSI {recovery['extreme']:.1f} -> {recovery['current']:.1f}",
            reversal["name"],
            "Grüne Bestätigung" if side == "LONG" else "Rote Bestätigung",
            "Bullish Strukturbruch danach" if side == "LONG" else "Bearish Strukturbruch danach",
        ] + extras

        return {
            "side": side,
            "trigger": " + ".join(reasons),
            "strong": bool(extras),
        }

    return None

def create_trade(symbol, setup, data):
    entry = data[-1]["close"]
    a = atr(data, ATR_PERIOD)
    if a <= 0:
        return None

    if setup["side"] == "LONG":
        stop = entry - ATR_MULTIPLIER * a
        risk = entry - stop
        target = entry + RR * risk
    else:
        stop = entry + ATR_MULTIPLIER * a
        risk = stop - entry
        target = entry - RR * risk

    if risk <= 0:
        return None

    ts = now()
    return {
        "time": ts,
        "strategy": STRATEGY,
        "symbol": symbol,
        "side": setup["side"],
        "timeframe": "15m",
        "setup_type": "RSI Recovery + Reversal + Structure" + (" + QUALITY" if setup["strong"] else ""),
        "entry": num(entry),
        "stop": num(stop),
        "tp1": "",
        "tp2": num(target),
        "tp3": "",
        "planned_rr": "1:2",
        "trigger": setup["trigger"],
        "status": "OPEN",
        "last_checked": ts,
    }

def load():
    if not os.path.exists(JOURNAL):
        return []
    with open(JOURNAL, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        if not row.get("strategy"):
            row["strategy"] = "CURRENT"
    return rows

def save(rows):
    with open(JOURNAL, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})

def v5_rows(rows):
    return [r for r in rows if r.get("strategy") == STRATEGY]

def check_open_trade(row):
    checkpoint = row.get("last_checked", "") or row["time"]
    checkpoint_ms = int(
        datetime.fromisoformat(checkpoint.replace("Z", "+00:00")).timestamp() * 1000
    )

    data = candles(row["symbol"], "15m", 100)
    relevant = [c for c in data if c["ts"] > checkpoint_ms]

    if not relevant:
        return row["status"], checkpoint

    stop = flt(row["stop"])
    target = flt(row["tp2"])
    status = "OPEN"
    last_ts = checkpoint_ms

    for c in relevant:
        last_ts = c["ts"]

        if row["side"] == "LONG":
            stop_hit = c["low"] <= stop
            target_hit = c["high"] >= target
        else:
            stop_hit = c["high"] >= stop
            target_hit = c["low"] <= target

        if stop_hit and target_hit:
            status = "UNCLEAR"
            break
        if target_hit:
            status = "TP2"
            break
        if stop_hit:
            status = "STOP"
            break

    return status, datetime.fromtimestamp(
        last_ts / 1000, tz=timezone.utc
    ).isoformat()

def update_v5_journal(rows):
    indexes = [
        i for i, r in enumerate(rows)
        if r.get("strategy") == STRATEGY and r.get("status") == "OPEN"
    ]

    if not indexes:
        return 0

    changed = 0

    with ThreadPoolExecutor(
        max_workers=min(MAX_WORKERS, len(indexes))
    ) as executor:

        futures = {
            executor.submit(check_open_trade, rows[i]): i
            for i in indexes
        }

        for future in as_completed(futures):
            i = futures[future]

            try:
                status, last_checked = future.result()

                if status != rows[i]["status"]:
                    print(
                        "V5 Journal:",
                        rows[i]["symbol"],
                        rows[i]["status"],
                        "->",
                        status,
                    )
                    rows[i]["status"] = status
                    changed += 1

                rows[i]["last_checked"] = last_checked

            except Exception as e:
                print(
                    "V5 Journal Fehler:",
                    rows[i].get("symbol"),
                    e,
                )

    return changed

def notify_new_setup(trade, number):
    direction = "🟢 LONG" if trade["side"] == "LONG" else "🔴 SHORT"
    quality = "🔥 STARKES SIGNAL" if "+ QUALITY" in trade["setup_type"] else "STANDARD SIGNAL"
    state = (
        "ÜBERVERKAUFT → BULLISHE UMKEHR"
        if trade["side"] == "LONG"
        else "ÜBERKAUFT → BEARISHE UMKEHR"
    )

    message = (
        "🚨 NEUES V5 MEAN-REVERSION SETUP\n\n"
        f"{quality}\n"
        f"Trade: {number}/{TEST_LIMIT}\n"
        f"Paar: {trade['symbol']}\n"
        f"Richtung: {direction}\n"
        f"Markt: {state}\n"
        "Zeitrahmen: 15m\n\n"
        f"Bestätigung:\n{trade['trigger']}\n\n"
        f"Entry: {trade['entry']}\n"
        f"Stop-Loss (2x ATR): {trade['stop']}\n"
        f"Take-Profit 2R: {trade['tp2']}\n\n"
        "Risk/Reward: 1:2"
    )

    telegram(message)

def scan_market(symbol):
    data = candles(symbol, "15m", 120)
    setup = find_setup(data)
    if not setup:
        return None
    return create_trade(symbol, setup, data)

def stats(rows):
    trades = v5_rows(rows)
    wins = sum(r.get("status") == "TP2" for r in trades)
    losses = sum(r.get("status") == "STOP" for r in trades)
    unclear = sum(r.get("status") == "UNCLEAR" for r in trades)
    opened = sum(r.get("status") == "OPEN" for r in trades)
    completed = wins + losses

    return {
        "total": len(trades),
        "wins": wins,
        "losses": losses,
        "unclear": unclear,
        "open": opened,
        "winrate": wins / completed * 100 if completed else 0,
    }

def main():
    start = time.monotonic()

    print("V5 Mean Reversion Scanner gestartet")
    print("Parallel Workers:", MAX_WORKERS)

    rows = load()

    updates = update_v5_journal(rows)

    count = len(v5_rows(rows))
    print("V5 Signale bisher:", count, "/", TEST_LIMIT)

    if count >= TEST_LIMIT:
        save(rows)
        result = stats(rows)
        print("V5 Testlimit erreicht. Keine neuen Entries.")
        print("V5 offen:", result["open"])
        print("V5 Gewinner 2R:", result["wins"])
        print("V5 Stops:", result["losses"])
        print("V5 Winrate:", f"{result['winrate']:.1f}%")
        return

    market_list = markets()
    print("Geeignete Crypto-Märkte:", len(market_list))

    existing_open = {
        (r.get("symbol"), r.get("side"))
        for r in rows
        if r.get("strategy") == STRATEGY and r.get("status") == "OPEN"
    }

    found = []
    scanned = 0
    errors = 0
    new_signals = 0

    print("Paralleler Markt-Scan gestartet...")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(scan_market, symbol): symbol
            for symbol in market_list
        }

        completed = 0

        for future in as_completed(futures):
            symbol = futures[future]
            completed += 1

            try:
                trade = future.result()
                scanned += 1

                if trade:
                    found.append(trade)

            except Exception as e:
                errors += 1
                print("SCAN ERROR:", symbol, e)

            if completed % 25 == 0:
                print(
                    "Progress:",
                    completed,
                    "/",
                    len(market_list),
                )

    found.sort(
        key=lambda trade: (
            trade["symbol"],
            trade["side"],
        )
    )

    print("Gültige Setups gefunden:", len(found))

    for trade in found:
        if len(v5_rows(rows)) >= TEST_LIMIT:
            print("100 V5 Signale erreicht.")
            break

        key = (
            trade["symbol"],
            trade["side"],
        )

        if key in existing_open:
            continue

        rows.append(trade)
        existing_open.add(key)
        new_signals += 1

        number = len(v5_rows(rows))

        print(
            "NEW V5:",
            number,
            "/",
            TEST_LIMIT,
            trade["symbol"],
            trade["side"],
            trade["trigger"],
        )

        # Telegram NUR bei einem neuen V5-Setup.
        notify_new_setup(
            trade,
            number,
        )

    save(rows)

    result = stats(rows)

    print("--------------------------------")
    print("V5 SCAN BEENDET")
    print("Märkte geprüft:", scanned)
    print("Fehler:", errors)
    print("Neue V5 Signale:", new_signals)
    print("V5 Journal Updates:", updates)
    print("V5 Trades gesamt:", result["total"])
    print("V5 offen:", result["open"])
    print("V5 Gewinner 2R:", result["wins"])
    print("V5 Stops:", result["losses"])
    print("V5 Unklar:", result["unclear"])
    print("V5 Winrate:", f"{result['winrate']:.1f}%")
    print("Gesamtlaufzeit:", f"{time.monotonic() - start:.1f} Sekunden")
    print("--------------------------------")

if __name__ == "__main__":
    main()
