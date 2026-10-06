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
STRATEGY = "V7_STRONG_REVERSAL_DUAL_ATR"

MAX_MARKETS = 400
MAX_WORKERS = 8
TEST_LIMIT = 100
API_TIMEOUT = 6
API_RETRIES = 2

RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 70

ATR_PERIOD = 14
ATR_MULTIPLIER_15 = 1.5
ATR_MULTIPLIER_20 = 2.0
RR = 2.0

# V7 quality filters
ENGULFING_BODY_MULTIPLIER = 1.5
BREAK_BODY_MIN_RATIO = 0.60
BREAK_CLOSE_EDGE_RATIO = 0.25

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# "stop/tp2/status/max_r" remain the 2.0 ATR version for compatibility.
# The *_15 fields track the independent 1.5 ATR version.
FIELDS = [
    "time", "strategy", "symbol", "side", "timeframe", "setup_type",
    "entry",
    "stop_15", "tp2_15", "status_15", "max_r_15",
    "stop", "tp1", "tp2", "tp3", "planned_rr",
    "trigger", "status", "last_checked", "max_r"
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
            headers={"User-Agent": "CryptoScanner/V7"}
        )
        with urllib.request.urlopen(req, timeout=8) as response:
            result = json.loads(response.read().decode())
        if not result.get("ok"):
            raise RuntimeError(result)
        print("Telegram: neues V7 Setup gesendet")
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
                headers={"User-Agent": "Mozilla/5.0 CryptoScanner/V7"}
            )
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as response:
                result = json.loads(response.read().decode())
            if result.get("code") != "0":
                raise RuntimeError(result)
            return result.get("data", [])
        except Exception as e:
            last_error = e
            if attempt < API_RETRIES:
                time.sleep(0.5 * (attempt + 1))
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
        "instId": symbol,
        "bar": bar,
        "limit": str(limit)
    })

    out = []
    for c in reversed(raw):
        if len(c) > 8 and c[8] != "1":
            continue
        out.append({
            "ts": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5])
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

    ag = sum(gains) / period
    al = sum(losses) / period
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


# User-defined Fib rule remains unchanged:
# GREEN body fully inside upper 0-0.382 zone.
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


# Mirrored rule remains unchanged:
# RED body fully inside lower 0-0.382 zone.
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


# Strong engulfing:
# full body engulf + current body at least 1.5x previous body.
def bullish_engulfing(p, c):
    prev_body = body(p)
    return (
        bearish(p)
        and bullish(c)
        and prev_body > 0
        and c["open"] <= p["close"]
        and c["close"] >= p["open"]
        and body(c) >= ENGULFING_BODY_MULTIPLIER * prev_body
    )


def bearish_engulfing(p, c):
    prev_body = body(p)
    return (
        bullish(p)
        and bearish(c)
        and prev_body > 0
        and c["open"] >= p["close"]
        and c["close"] <= p["open"]
        and body(c) >= ENGULFING_BODY_MULTIPLIER * prev_body
    )


def rsi_extreme_before_reversal(rsis, side, reversal_index, lookback=10):
    start = max(RSI_PERIOD, reversal_index - lookback + 1)
    values = [x for x in rsis[start:reversal_index + 1] if x is not None]

    if not values:
        return None

    if side == "LONG":
        extreme = min(values)
        return extreme if extreme <= RSI_OVERSOLD else None

    extreme = max(values)
    return extreme if extreme >= RSI_OVERBOUGHT else None


def recent_reversal(data, side):
    if len(data) < 5:
        return None

    # Reversal must be one of the last two completed 15m candles.
    for i in range(len(data) - 2, len(data)):
        c, p = data[i], data[i - 1]

        if side == "LONG":
            if not bullish(c):
                continue
            if bullish_engulfing(p, c):
                return {"name": "Strong Bullish Engulfing 1.5x", "index": i}
            if bullish_fib_hammer(c):
                return {"name": "Bullish Fib-0.382 Hammer", "index": i}

        else:
            if not bearish(c):
                continue
            if bearish_engulfing(p, c):
                return {"name": "Strong Bearish Engulfing 1.5x", "index": i}
            if bearish_fib_pinbar(c):
                return {"name": "Bearish Fib-0.382 Pinbar", "index": i}

    return None


def strong_break_candle(c, side):
    r = candle_range(c)
    body_ratio = body(c) / r

    if body_ratio < BREAK_BODY_MIN_RATIO:
        return False

    if side == "LONG":
        if not bullish(c):
            return False
        # Close must be in top 25% of candle range.
        close_floor = c["high"] - BREAK_CLOSE_EDGE_RATIO * r
        return c["close"] >= close_floor

    if not bearish(c):
        return False

    # Close must be in bottom 25% of candle range.
    close_ceiling = c["low"] + BREAK_CLOSE_EDGE_RATIO * r
    return c["close"] <= close_ceiling


def structure_break_after(data, side, reversal_index):
    if len(data) < 5:
        return None

    for i in range(reversal_index + 1, len(data)):
        if i < 3:
            continue

        c = data[i]
        prev = data[i - 3:i]

        if not strong_break_candle(c, side):
            continue

        if side == "LONG":
            structure = max(x["high"] for x in prev)
            if c["close"] > structure:
                return i
        else:
            structure = min(x["low"] for x in prev)
            if c["close"] < structure:
                return i

    return None


def volume_confirmation(data):
    if len(data) < 22:
        return False

    prev = data[-21:-1]
    avg = sum(x["volume"] for x in prev) / len(prev)
    return avg > 0 and data[-1]["volume"] >= avg * 1.20


def find_setup(data):
    if len(data) < 50:
        return None

    rsis = rsi_series([x["close"] for x in data], RSI_PERIOD)
    if not rsis:
        return None

    for side in ("LONG", "SHORT"):
        reversal = recent_reversal(data, side)
        if not reversal:
            continue

        extreme = rsi_extreme_before_reversal(
            rsis, side, reversal["index"], lookback=10
        )
        if extreme is None:
            continue

        break_index = structure_break_after(data, side, reversal["index"])
        if break_index is None:
            continue

        reasons = [
            f"RSI Extrem {extreme:.1f}",
            reversal["name"],
            "Starker grüner Close Above"
            if side == "LONG"
            else "Starker roter Close Below",
            "Break-Body >= 60% + Close im oberen 25%"
            if side == "LONG"
            else "Break-Body >= 60% + Close im unteren 25%",
        ]

        volume_ok = volume_confirmation(data)
        if volume_ok:
            reasons.append("Volumen bestätigt")

        return {
            "side": side,
            "trigger": " + ".join(reasons),
            "strong": volume_ok,
        }

    return None


def create_trade(symbol, setup, data):
    entry = data[-1]["close"]
    a = atr(data, ATR_PERIOD)

    if a <= 0:
        return None

    if setup["side"] == "LONG":
        stop_15 = entry - ATR_MULTIPLIER_15 * a
        risk_15 = entry - stop_15
        target_15 = entry + RR * risk_15

        stop_20 = entry - ATR_MULTIPLIER_20 * a
        risk_20 = entry - stop_20
        target_20 = entry + RR * risk_20
    else:
        stop_15 = entry + ATR_MULTIPLIER_15 * a
        risk_15 = stop_15 - entry
        target_15 = entry - RR * risk_15

        stop_20 = entry + ATR_MULTIPLIER_20 * a
        risk_20 = stop_20 - entry
        target_20 = entry - RR * risk_20

    if risk_15 <= 0 or risk_20 <= 0:
        return None

    ts = now()

    return {
        "time": ts,
        "strategy": STRATEGY,
        "symbol": symbol,
        "side": setup["side"],
        "timeframe": "15m",
        "setup_type": (
            "RSI Extreme + Strong Reversal + Strong Structure Break"
            + (" + VOLUME" if setup["strong"] else "")
        ),
        "entry": num(entry),

        "stop_15": num(stop_15),
        "tp2_15": num(target_15),
        "status_15": "OPEN",
        "max_r_15": "0",

        # 2.0 ATR stays in legacy-compatible columns.
        "stop": num(stop_20),
        "tp1": "",
        "tp2": num(target_20),
        "tp3": "",
        "planned_rr": "1:2",

        "trigger": setup["trigger"],
        "status": "OPEN",
        "last_checked": ts,
        "max_r": "0",
    }


def load():
    if not os.path.exists(JOURNAL):
        return []

    with open(JOURNAL, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    for row in rows:
        if not row.get("strategy"):
            row["strategy"] = "CURRENT"

        for field in FIELDS:
            if field not in row:
                row[field] = ""

    return rows


def save(rows):
    with open(JOURNAL, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()

        for row in rows:
            writer.writerow({k: row.get(k, "") for k in FIELDS})


def strategy_rows(rows):
    return [r for r in rows if r.get("strategy") == STRATEGY]


def evaluate_variant(c, side, entry, stop, target, max_r):
    risk = abs(entry - stop)

    if risk <= 0:
        return "OPEN", max_r

    if side == "LONG":
        favorable_r = max(0.0, (c["high"] - entry) / risk)
        stop_hit = c["low"] <= stop
        target_hit = c["high"] >= target
    else:
        favorable_r = max(0.0, (entry - c["low"]) / risk)
        stop_hit = c["high"] >= stop
        target_hit = c["low"] <= target

    max_r = max(max_r, favorable_r)

    # OHLC cannot tell which came first if SL and TP are both touched
    # inside the same 15m candle.
    if stop_hit and target_hit:
        return "UNCLEAR", max_r
    if target_hit:
        return "TP2", max_r
    if stop_hit:
        return "STOP", max_r

    return "OPEN", max_r


def check_open_trade(row):
    checkpoint = row.get("last_checked", "") or row["time"]
    checkpoint_ms = int(
        datetime.fromisoformat(
            checkpoint.replace("Z", "+00:00")
        ).timestamp() * 1000
    )

    data = candles(row["symbol"], "15m", 100)
    relevant = [c for c in data if c["ts"] > checkpoint_ms]

    if not relevant:
        return (
            row.get("status_15", "OPEN"),
            row.get("max_r_15", "0"),
            row.get("status", "OPEN"),
            row.get("max_r", "0"),
            checkpoint,
        )

    entry = flt(row["entry"])

    status_15 = row.get("status_15", "OPEN") or "OPEN"
    max_r_15 = flt(row.get("max_r_15"), 0.0)
    stop_15 = flt(row.get("stop_15"))
    target_15 = flt(row.get("tp2_15"))

    status_20 = row.get("status", "OPEN") or "OPEN"
    max_r_20 = flt(row.get("max_r"), 0.0)
    stop_20 = flt(row.get("stop"))
    target_20 = flt(row.get("tp2"))

    last_ts = checkpoint_ms

    for c in relevant:
        last_ts = c["ts"]

        if status_15 == "OPEN":
            status_15, max_r_15 = evaluate_variant(
                c, row["side"], entry, stop_15, target_15, max_r_15
            )

        if status_20 == "OPEN":
            status_20, max_r_20 = evaluate_variant(
                c, row["side"], entry, stop_20, target_20, max_r_20
            )

        if status_15 != "OPEN" and status_20 != "OPEN":
            break

    return (
        status_15,
        num(max_r_15),
        status_20,
        num(max_r_20),
        datetime.fromtimestamp(
            last_ts / 1000, tz=timezone.utc
        ).isoformat(),
    )


def update_journal(rows):
    indexes = [
        i for i, r in enumerate(rows)
        if r.get("strategy") == STRATEGY
        and (
            r.get("status_15") == "OPEN"
            or r.get("status") == "OPEN"
        )
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
                (
                    status_15,
                    max_r_15,
                    status_20,
                    max_r_20,
                    last_checked,
                ) = future.result()

                old = (
                    rows[i].get("status_15", ""),
                    rows[i].get("max_r_15", ""),
                    rows[i].get("status", ""),
                    rows[i].get("max_r", ""),
                )

                rows[i]["status_15"] = status_15
                rows[i]["max_r_15"] = max_r_15
                rows[i]["status"] = status_20
                rows[i]["max_r"] = max_r_20
                rows[i]["last_checked"] = last_checked

                new = (
                    status_15,
                    max_r_15,
                    status_20,
                    max_r_20,
                )

                if new != old:
                    changed += 1
                    print(
                        "Journal:",
                        rows[i]["symbol"],
                        "| 1.5ATR:", status_15, "max_r:", max_r_15,
                        "| 2.0ATR:", status_20, "max_r:", max_r_20,
                    )

            except Exception as e:
                print("Journal Fehler:", rows[i].get("symbol"), e)

    return changed


def notify_new_setup(trade, number):
    direction = "🟢 LONG" if trade["side"] == "LONG" else "🔴 SHORT"
    quality = (
        "🔥 VOLUMEN BESTÄTIGT"
        if "+ VOLUME" in trade["setup_type"]
        else "STANDARD SIGNAL"
    )

    state = (
        "ÜBERVERKAUFT → BULLISHE UMKEHR"
        if trade["side"] == "LONG"
        else "ÜBERKAUFT → BEARISHE UMKEHR"
    )

    message = (
        "🚨 NEUES V7 SETUP\n\n"
        f"{quality}\n"
        f"Trade: {number}/{TEST_LIMIT}\n"
        f"Paar: {trade['symbol']}\n"
        f"Richtung: {direction}\n"
        f"Markt: {state}\n"
        "Zeitrahmen: 15m\n\n"
        f"Bestätigung:\n{trade['trigger']}\n\n"
        f"Entry: {trade['entry']}\n\n"
        "TEST A — 1.5x ATR\n"
        f"Stop: {trade['stop_15']}\n"
        f"Take-Profit 2R: {trade['tp2_15']}\n\n"
        "TEST B — 2.0x ATR\n"
        f"Stop: {trade['stop']}\n"
        f"Take-Profit 2R: {trade['tp2']}\n\n"
        "Beide Varianten werden separat im Journal ausgewertet."
    )

    telegram(message)


def scan_market(symbol):
    data = candles(symbol, "15m", 120)
    setup = find_setup(data)

    if not setup:
        return None

    return create_trade(symbol, setup, data)


def variant_stats(rows, variant):
    trades = strategy_rows(rows)

    if variant == "15":
        status_field = "status_15"
    else:
        status_field = "status"

    wins = sum(r.get(status_field) == "TP2" for r in trades)
    losses = sum(r.get(status_field) == "STOP" for r in trades)
    unclear = sum(r.get(status_field) == "UNCLEAR" for r in trades)
    opened = sum(r.get(status_field) == "OPEN" for r in trades)
    completed = wins + losses

    return {
        "total": len(trades),
        "wins": wins,
        "losses": losses,
        "unclear": unclear,
        "open": opened,
        "completed": completed,
        "winrate": wins / completed * 100 if completed else 0,
        "net_r": wins * RR - losses,
    }


def ratio_report(rows, variant):
    if variant == "15":
        status_field = "status_15"
        max_r_field = "max_r_15"
        label = "1.5 ATR"
    else:
        status_field = "status"
        max_r_field = "max_r"
        label = "2.0 ATR"

    trades = [
        r for r in strategy_rows(rows)
        if r.get(status_field) in ("TP2", "STOP")
    ]

    if not trades:
        return

    print(f"Ratio-Auswertung {label} anhand max_r:")

    for n in range(10, 21):
        rr = n / 10
        wins = sum(
            flt(r.get(max_r_field), 0) >= rr
            for r in trades
        )
        losses = len(trades) - wins
        winrate = wins / len(trades) * 100
        net_r = wins * rr - losses

        print(
            f"{label} | 1:{rr:.1f} | Wins {wins}/{len(trades)} | "
            f"Winrate {winrate:.1f}% | Ergebnis {net_r:+.1f}R"
        )


def print_comparison(rows):
    s15 = variant_stats(rows, "15")
    s20 = variant_stats(rows, "20")

    print("========== ATR STOP VERGLEICH ==========")
    print(
        f"1.5 ATR | TP2 {s15['wins']} | STOP {s15['losses']} | "
        f"OPEN {s15['open']} | UNCLEAR {s15['unclear']} | "
        f"WR {s15['winrate']:.1f}% | Netto {s15['net_r']:+.1f}R"
    )
    print(
        f"2.0 ATR | TP2 {s20['wins']} | STOP {s20['losses']} | "
        f"OPEN {s20['open']} | UNCLEAR {s20['unclear']} | "
        f"WR {s20['winrate']:.1f}% | Netto {s20['net_r']:+.1f}R"
    )
    print("========================================")


def main():
    start = time.monotonic()

    print("V7 Strong Reversal + Dual ATR Test gestartet")
    print("Parallel Workers:", MAX_WORKERS)
    print("Engulfing Body: mindestens 1.5x vorheriger Body")
    print("Breakout Body: mindestens 60% der Candle-Range")
    print("LONG Close: obere 25% | SHORT Close: untere 25%")
    print("SL Test parallel: 1.5 ATR vs 2.0 ATR")
    print("FVG: AUS | Triangle/Chart-Pattern: AUS")

    rows = load()
    updates = update_journal(rows)

    count = len(strategy_rows(rows))
    print("V7 Signale bisher:", count, "/", TEST_LIMIT)

    if count >= TEST_LIMIT:
        save(rows)
        print("V7 Testlimit erreicht. Keine neuen Entries.")
        print_comparison(rows)
        ratio_report(rows, "15")
        ratio_report(rows, "20")
        return

    market_list = markets()
    print("Geeignete Crypto-Märkte:", len(market_list))

    existing_open = {
        (r.get("symbol"), r.get("side"))
        for r in rows
        if r.get("strategy") == STRATEGY
        and (
            r.get("status_15") == "OPEN"
            or r.get("status") == "OPEN"
        )
    }

    found = []
    scanned = 0
    errors = 0
    new_signals = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(scan_market, symbol): symbol
            for symbol in market_list
        }

        for completed, future in enumerate(
            as_completed(futures), start=1
        ):
            symbol = futures[future]

            try:
                trade = future.result()
                scanned += 1
                if trade:
                    found.append(trade)
            except Exception as e:
                errors += 1
                print("SCAN ERROR:", symbol, e)

            if completed % 25 == 0:
                print("Progress:", completed, "/", len(market_list))

    found.sort(key=lambda trade: (trade["symbol"], trade["side"]))
    print("Gültige V7 Setups gefunden:", len(found))

    for trade in found:
        if len(strategy_rows(rows)) >= TEST_LIMIT:
            print("100 V7 Signale erreicht.")
            break

        key = (trade["symbol"], trade["side"])
        if key in existing_open:
            continue

        rows.append(trade)
        existing_open.add(key)
        new_signals += 1
        number = len(strategy_rows(rows))

        print(
            "NEW V7:",
            number, "/", TEST_LIMIT,
            trade["symbol"],
            trade["side"],
            trade["trigger"],
        )

        notify_new_setup(trade, number)

    save(rows)

    print("--------------------------------")
    print("V7 SCAN BEENDET")
    print("Märkte geprüft:", scanned)
    print("Fehler:", errors)
    print("Neue V7 Signale:", new_signals)
    print("V7 Journal Updates:", updates)
    print("V7 Trades gesamt:", len(strategy_rows(rows)))
    print_comparison(rows)
    ratio_report(rows, "15")
    ratio_report(rows, "20")
    print(
        "Gesamtlaufzeit:",
        f"{time.monotonic() - start:.1f} Sekunden"
    )
    print("--------------------------------")


if __name__ == "__main__":
    main()
