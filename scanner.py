#!/usr/bin/env python3
"""V8: 15m trend + structural pullback + candle + support/resistance room.

Public OKX 15m candles. Top 500 eligible OKX crypto USDT swaps ranked by 24h USDT turnover.
Simulated 1R / 1.5R / 2R outcomes, one common structure-based stop.
Only newly confirmed setups are sent to Telegram. No trading is executed.
Standard-library only; compatible with existing GitHub Actions workflow.
"""

import csv
import json
import math
import os
import statistics
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

STRATEGY = "V8_TREND_PULLBACK_SR"
JOURNAL = "signals.csv"  # Must match current GitHub workflow's git add signals.csv
TEST_LIMIT = 100
MAX_MARKETS = 500
MAX_WORKERS = 8
API_TIMEOUT = 8
API_RETRIES = 2

PULLBACK_BARS = 6
SWING_SPAN = 2
STOP_BUFFER = 0.001  # 0.10% beyond pullback low/high
CONFIRM_BODY_FRACTION = 0.60
CONFIRM_CLOSE_EDGE = 0.25

ZONE_LOOKBACK = 145
ZONE_MIN_TOUCHES = 2
ZONE_TOUCH_SPACING_BARS = 4
MIN_FREE_ROOM_R = 1.0

OKX = "https://www.okx.com"
COINGECKO = "https://api.coingecko.com/api/v3"

STABLE = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE", "USDS", "PYUSD",
    "USD1", "FRAX", "LUSD", "USDP", "RLUSD", "SUSD", "CRVUSD", "EURC",
}
MEME = {
    "DOGE", "SHIB", "PEPE", "BONK", "WIF", "FLOKI", "BRETT", "MOG",
    "POPCAT", "MEW", "TURBO", "NEIRO", "BABYDOGE", "MEME", "BOME",
    "PNUT", "GOAT", "ACT", "PENGU", "FARTCOIN", "TRUMP", "MELANIA",
    "SPX", "GIGA", "PONKE", "DEGEN", "DOGS", "USELESS", "SNEK",
}

NEW_FIELDS = [
    "time", "strategy", "symbol", "side", "timeframe", "setup_type",
    "entry", "stop", "tp_1r", "status_1r", "tp_15r", "status_15r",
    "tp_2r", "status_2r", "trigger", "last_checked",
]


def request_json(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    error = None
    for attempt in range(API_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "CryptoScanner-V8/1.1", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception as exc:
            error = exc
            if attempt < API_RETRIES:
                time.sleep(0.5 * (attempt + 1))
    raise error


def okx_crypto_usdt_swaps():
    result = request_json(OKX + "/api/v5/public/instruments", {"instType": "SWAP"})
    if result.get("code") != "0":
        raise RuntimeError("OKX instruments: " + str(result.get("msg")))
    symbols = {}
    for item in result.get("data", []):
        name = item.get("instId", "")
        if (
            item.get("state") == "live"
            and name.endswith("-USDT-SWAP")
            and str(item.get("instCategory", "")) == "1"  # Crypto, not stock/ETF
        ):
            symbols[name[:-len("-USDT-SWAP")].upper()] = name
    return symbols


def coin_gecko_page(page, category=None):
    params = {
        "vs_currency": "usd", "order": "market_cap_desc", "per_page": 250,
        "page": page, "sparkline": "false",
    }
    if category:
        params["category"] = category
    result = request_json(COINGECKO + "/coins/markets", params)
    if not isinstance(result, list):
        raise ValueError("CoinGecko returned no market list")
    return result


def category_symbols(category):
    symbols = set()
    for page in (1, 2):
        try:
            results = coin_gecko_page(page, category)
        except Exception as exc:
            print("CoinGecko category unavailable:", category, str(exc))
            break
        symbols.update(str(x.get("symbol", "")).upper() for x in results)
        if len(results) < 250:
            break
    return symbols


def universe():
    """Rank ALL available OKX crypto USDT swaps by 24h USDT trade value.

    OKX reports derivative volCcy24h in BASE coins, so multiply by the
    current USDT price; raw coin volume would rank cheap coins incorrectly.
    Do not scan stock/ETF products, or known/category-tagged memes/stables.
    """
    instruments = okx_crypto_usdt_swaps()
    excluded = STABLE | MEME | category_symbols("stablecoins") | category_symbols("meme-token")

    response = request_json(OKX + "/api/v5/market/tickers", {"instType": "SWAP"})
    if response.get("code") != "0":
        raise RuntimeError("OKX tickers: " + str(response.get("msg")))

    turnover = {}
    for ticker in response.get("data", []):
        inst = ticker.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        base = inst[:-len("-USDT-SWAP")].upper()
        if instruments.get(base) != inst or base in excluded:
            continue
        try:
            last = float(ticker.get("last", 0))
            volume_base = float(ticker.get("volCcy24h", 0))
        except (ValueError, TypeError, OverflowError):
            continue
        trade_value_usdt = last * volume_base
        if not math.isfinite(trade_value_usdt) or trade_value_usdt <= 0:
            continue
        turnover[inst] = trade_value_usdt

    selected = sorted(turnover, key=lambda inst: (-turnover[inst], inst))[:MAX_MARKETS]
    print(
        "OKX crypto USDT swaps:", len(instruments),
        "| eligible with volume:", len(turnover),
        "| selected top volume:", len(selected),
        "| excluded meme/stable: category + blocklist",
    )
    return selected


def candles(inst, limit=200):
    result = request_json(
        OKX + "/api/v5/market/candles", {"instId": inst, "bar": "15m", "limit": limit}
    )
    if result.get("code") != "0":
        raise RuntimeError("OKX candles: " + str(result.get("msg")))
    rows = [
        {
            "ts": int(item[0]), "open": float(item[1]), "high": float(item[2]),
            "low": float(item[3]), "close": float(item[4]),
        }
        for item in result.get("data", [])
        if len(item) > 8 and str(item[8]) == "1"  # completed 15m only
    ]
    return sorted(rows, key=lambda x: x["ts"])


def swing_points(data, span=SWING_SPAN):
    highs, lows = [], []
    for idx in range(span, len(data) - span):
        cur = data[idx]
        before = data[idx - span:idx]
        after = data[idx + 1:idx + span + 1]
        if cur["high"] > max(x["high"] for x in before) and cur["high"] >= max(x["high"] for x in after):
            highs.append((idx, cur["high"]))
        if cur["low"] < min(x["low"] for x in before) and cur["low"] <= min(x["low"] for x in after):
            lows.append((idx, cur["low"]))
    return highs, lows


def trend_before_pullback(data):
    # Do not use the confirming candle or unfinished pullback as the basis of trend.
    historical = data[:-(PULLBACK_BARS + 1)]
    highs, lows = swing_points(historical)
    if len(highs) < 2 or len(lows) < 2:
        return None, None
    if highs[-1][1] > highs[-2][1] and lows[-1][1] > lows[-2][1]:
        return "LONG", lows[-1][1]
    if highs[-1][1] < highs[-2][1] and lows[-1][1] < lows[-2][1]:
        return "SHORT", highs[-1][1]
    return None, None


def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


def candle_body(c):
    return abs(c["close"] - c["open"])


def candle_range(c):
    return max(c["high"] - c["low"], 1e-12)


def strong_directional_candle(c, side):
    whole = candle_range(c)
    if candle_body(c) < CONFIRM_BODY_FRACTION * whole:
        return False
    if side == "LONG":
        return bullish(c) and c["close"] >= c["low"] + (1 - CONFIRM_CLOSE_EDGE) * whole
    return bearish(c) and c["close"] <= c["low"] + CONFIRM_CLOSE_EDGE * whole


def bullish_fib_hammer(c):
    # User's original Fib: 0 = high, 0.382 below. Whole GREEN body in 0–0.382.
    if not bullish(c):
        return False
    whole = candle_range(c)
    fib382 = c["high"] - 0.382 * whole
    lower_wick = min(c["open"], c["close"]) - c["low"]
    upper_wick = c["high"] - max(c["open"], c["close"])
    return (
        min(c["open"], c["close"]) >= fib382
        and lower_wick >= max(2 * candle_body(c), whole * 0.40)
        and upper_wick <= whole * 0.25
    )


def bearish_fib_pinbar(c):
    # User's original Fib mirror: 0 = low, 0.382 above. Whole RED body near low.
    if not bearish(c):
        return False
    whole = candle_range(c)
    fib382 = c["low"] + 0.382 * whole
    upper_wick = c["high"] - max(c["open"], c["close"])
    lower_wick = min(c["open"], c["close"]) - c["low"]
    return (
        max(c["open"], c["close"]) <= fib382
        and upper_wick >= max(2 * candle_body(c), whole * 0.40)
        and lower_wick <= whole * 0.25
    )


def zone_half_width(data):
    # Half-width scales with recent candle range, bounded to 0.2–1% of price.
    recent = data[-60:]
    if not recent:
        return 0
    price = recent[-1]["close"]
    typical = statistics.median(candle_range(c) for c in recent)
    return min(max(price * 0.002, typical * 0.5), price * 0.01)


def repeated_zones(data, side):
    """Group distinct swing-low touches into support and swing-high touches into resistance.

    A repeated zone requires >=2 swing pivot touches at least four candles apart.
    Zone bounds are derived only from historical completed bars.
    """
    historical = data[-ZONE_LOOKBACK - 1:-1]  # excludes confirmation candle
    if len(historical) < 20:
        return []
    highs, lows = swing_points(historical)
    points = lows if side == "SHORT" else highs
    width = zone_half_width(historical)
    if not width:
        return []

    clusters = []
    for bar, level in points:
        match = None
        for cluster in clusters:
            center = statistics.mean(price for _, price in cluster)
            if abs(level - center) <= width:
                match = cluster
                break
        if match is None:
            clusters.append([(bar, level)])
        else:
            match.append((bar, level))

    zones = []
    for cluster in clusters:
        distinct = []
        for bar, level in cluster:
            if not distinct or bar - distinct[-1][0] >= ZONE_TOUCH_SPACING_BARS:
                distinct.append((bar, level))
        if len(distinct) < ZONE_MIN_TOUCHES:
            continue
        center = statistics.mean(level for _, level in distinct)
        zones.append((center - width, center + width, len(distinct)))
    return zones


def enough_room_to_zone(data, side, entry, risk):
    """Block entry if the nearest tested opposing zone is closer than 1R.

    Shorts: support below. Longs: resistance above. Returns (allowed, details).
    """
    zones = repeated_zones(data, side)
    if side == "LONG":
        ahead = [(low, high, n) for low, high, n in zones if high > entry]
        if not ahead:
            return True, "no repeated resistance ahead"
        lower, upper, touches = min(ahead, key=lambda z: max(z[0] - entry, 0))
        distance = max(lower - entry, 0.0)
        label = "resistance"
    else:
        ahead = [(low, high, n) for low, high, n in zones if low < entry]
        if not ahead:
            return True, "no repeated support below"
        lower, upper, touches = max(ahead, key=lambda z: min(z[1] - entry, 0))
        distance = max(entry - upper, 0.0)
        label = "support"
    room_r = distance / risk
    return room_r >= MIN_FREE_ROOM_R, f"nearest {label} [{lower:.8g}, {upper:.8g}], {touches} tests, free {room_r:.2f}R"


def setup(data):
    if len(data) < 100:
        return None
    side, protected = trend_before_pullback(data)
    if not side:
        return None

    confirmation = data[-1]
    pb = data[-PULLBACK_BARS - 1:-1]
    strong = strong_directional_candle(confirmation, side)
    fib = bullish_fib_hammer(confirmation) if side == "LONG" else bearish_fib_pinbar(confirmation)
    if not (strong or fib):
        return None

    if side == "LONG":
        pullback_low = min(c["low"] for c in pb)
        retraced = sum(bearish(c) for c in pb) >= 2
        if not retraced or pullback_low <= protected:
            return None
        if confirmation["close"] <= pb[-1]["high"]:
            return None
        entry = confirmation["close"]
        stop = pullback_low * (1 - STOP_BUFFER)
        if stop >= entry:
            return None
        trigger = "HH+HL + pullback + GREEN"
    else:
        pullback_high = max(c["high"] for c in pb)
        retraced = sum(bullish(c) for c in pb) >= 2
        if not retraced or pullback_high >= protected:
            return None
        if confirmation["close"] >= pb[-1]["low"]:
            return None
        entry = confirmation["close"]
        stop = pullback_high * (1 + STOP_BUFFER)
        if stop <= entry:
            return None
        trigger = "LH+LL + pullback + RED"

    risk = abs(entry - stop)
    if risk <= 0:
        return None
    allowed, zone_info = enough_room_to_zone(data, side, entry, risk)
    if not allowed:
        return None
    trigger += " Fib-0.382" if fib else " strong-body"
    trigger += " | S/R: " + zone_info

    direction = 1 if side == "LONG" else -1
    targets = tuple(entry + direction * risk * x for x in (1.0, 1.5, 2.0))
    if min(targets) <= 0:
        return None
    return side, entry, stop, targets, trigger


def read_journal():
    if not os.path.isfile(JOURNAL):
        return [], list(NEW_FIELDS)
    with open(JOURNAL, newline="", encoding="utf-8") as file:
        reader = csv.DictReader(file)
        original = list(reader.fieldnames or [])
        rows = list(reader)
    return rows, original + [col for col in NEW_FIELDS if col not in original]


def write_journal(rows, headers):
    # Preserve ALL V5/V6/V7 columns and trades, append only missing V8 columns.
    temp = JOURNAL + ".tmp"
    with open(temp, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(temp, JOURNAL)


def fprice(x):
    return f"{x:.12f}".rstrip("0").rstrip(".")


def update_trade(row):
    if not any(row.get(key) == "OPEN" for key in ("status_1r", "status_15r", "status_2r")):
        return
    try:
        checkpoint = int(row.get("last_checked") or "0")
        cs = [c for c in candles(row["symbol"], 100) if c["ts"] > checkpoint]
        for status_field, target_field in (
            ("status_1r", "tp_1r"), ("status_15r", "tp_15r"), ("status_2r", "tp_2r")
        ):
            if row.get(status_field) != "OPEN":
                continue
            sl, target = float(row["stop"]), float(row[target_field])
            for candle in cs:
                stop_hit = (candle["low"] <= sl if row["side"] == "LONG" else candle["high"] >= sl)
                tp_hit = (candle["high"] >= target if row["side"] == "LONG" else candle["low"] <= target)
                if stop_hit and tp_hit:
                    row[status_field] = "UNCLEAR"  # 15m candle has unknown intrabar ordering
                    break
                if stop_hit:
                    row[status_field] = "STOP"
                    break
                if tp_hit:
                    row[status_field] = "TP"
                    break
        if cs:
            row["last_checked"] = str(cs[-1]["ts"])
    except Exception as exc:
        print("Journal update error", row.get("symbol"), exc)


def check_symbol(inst):
    try:
        data = candles(inst)
        return inst, data[-1]["ts"], setup(data), None
    except Exception as exc:
        return inst, 0, None, str(exc)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode("utf-8")
        req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=API_TIMEOUT):
            pass
    except Exception as exc:
        print("Telegram error:", exc)


def main():
    rows, headers = read_journal()
    current = [r for r in rows if r.get("strategy") == STRATEGY]
    open_rows = [r for r in current if "OPEN" in (
        r.get("status_1r"), r.get("status_15r"), r.get("status_2r")
    )]
    if open_rows:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            list(pool.map(update_trade, open_rows))

    if len(current) >= TEST_LIMIT:
        write_journal(rows, headers)
        print("V8 support/resistance: test limit reached, still tracking open trades")
        return

    symbols = universe()
    seen = {(r.get("symbol"), r.get("time")) for r in current}
    new, errors = [], 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(check_symbol, inst) for inst in symbols]
        for future in as_completed(futures):
            inst, ts, result, error = future.result()
            if error:
                errors += 1
                print("API error", inst, error)
                continue
            if not result or len(current) + len(new) >= TEST_LIMIT:
                continue
            side, entry, stop, targets, trigger = result
            signal_time = datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()
            if (inst, signal_time) in seen:
                continue
            row = {col: "" for col in headers}
            row.update({
                "time": signal_time, "strategy": STRATEGY, "symbol": inst,
                "side": side, "timeframe": "15m",
                "setup_type": "Trend + Pullback + S/R confirmation",
                "entry": fprice(entry), "stop": fprice(stop),
                "tp_1r": fprice(targets[0]), "status_1r": "OPEN",
                "tp_15r": fprice(targets[1]), "status_15r": "OPEN",
                "tp_2r": fprice(targets[2]), "status_2r": "OPEN",
                "trigger": trigger, "last_checked": str(ts),
            })
            new.append(row)
            seen.add((inst, signal_time))

    rows.extend(new)
    write_journal(rows, headers)
    for row in new:
        send_telegram(
            f"V8 TREND-PULLBACK {row['side']} {row['symbol']}\n"
            f"{row['trigger']}\n"
            f"Entry: {row['entry']} | SL: {row['stop']}\n"
            f"TP 1R: {row['tp_1r']}\n"
            f"TP 1.5R: {row['tp_15r']}\n"
            f"TP 2R: {row['tp_2r']}"
        )
    print("V8 S/R | scanned:", len(symbols), "errors:", errors,
          "new signals:", len(new), "total:", len(current) + len(new))


if __name__ == "__main__":
    main()
