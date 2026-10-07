import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# V10 MEAN REVERSION – V4 + BTC REGIME FILTER
# RSI Extrem -> Recovery -> Reversal -> Strukturbruch
# -> struktureller Stop + 0.15 ATR -> Take Profit 2R
# ============================================================

BASE = "https://www.okx.com"
JOURNAL = "signals.csv"
STRATEGY = "V10_MEAN_REVERSION"

MAX_MARKETS = 400
TEST_LIMIT = 100

# V10 filters
LONG_MIN_STOP_PCT = 0.75
STOP_COOLDOWN_HOURS = 12
BTC_SYMBOL = "BTC-USDT-SWAP"
BTC_BAR = "1H"
BTC_FAST_EMA = 20
BTC_SLOW_EMA = 50
BTC_MOMENTUM_BARS = 3
BTC_STRONG_MOVE_PCT = 0.75
PAUSE = 0.035

RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
RSI_LONG_RECOVERY = 40
RSI_SHORT_RECOVERY = 60

RR = 2.0
ATR_STOP_BUFFER = 0.15

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FIELDS = [
    "time", "strategy", "symbol", "side", "timeframe", "setup_type",
    "entry", "stop", "tp1", "tp2", "tp3", "planned_rr",
    "trigger", "status", "last_checked"
]

MEME_BASES = {
    "DOGE","SHIB","PEPE","BONK","FLOKI","WIF","BOME","MEME","TURBO",
    "NEIRO","BRETT","MOG","POPCAT","MEW","PONKE","SLERF","BABYDOGE",
    "DOGS","CAT","HIPPO","PNUT","GOAT","ACT","MOODENG","TRUMP","MELANIA"
}

STABLE_BASES = {
    "USDT","USDC","DAI","FDUSD","TUSD","USDE","PYUSD","USDS",
    "BUSD","USD0","FRAX"
}


def now():
    return datetime.now(timezone.utc).isoformat()


def num(value):
    return format(float(value), ".12g")


def flt(value, default=0.0):
    try:
        return float(value)
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

        request = urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data=data,
            headers={"User-Agent": "CryptoScanner/V10"}
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            result = json.loads(response.read().decode())

        if not result.get("ok"):
            raise RuntimeError(result)

        print("Telegram: neues V4-Setup gesendet")
        return True
    except Exception as error:
        print("Telegram Fehler:", error)
        return False


def api(path, params=None):
    if params:
        path += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(
        BASE + path,
        headers={"User-Agent": "Mozilla/5.0 CryptoScanner/V10"}
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode())

    if result.get("code") != "0":
        raise RuntimeError(result)

    return result.get("data", [])


def markets():
    instruments = api("/api/v5/public/instruments", {"instType": "SWAP"})
    tickers = api("/api/v5/market/tickers", {"instType": "SWAP"})

    volumes = {
        ticker.get("instId", ""): flt(ticker.get("volCcy24h"))
        for ticker in tickers
    }

    selected = []
    for instrument in instruments:
        symbol = instrument.get("instId", "")

        if not symbol.endswith("-USDT-SWAP"):
            continue
        if instrument.get("state") != "live":
            continue

        base = symbol.split("-")[0]
        if base in STABLE_BASES or base in MEME_BASES:
            continue

        volume = volumes.get(symbol, 0)
        if volume <= 0:
            continue

        selected.append((symbol, volume))

    selected.sort(key=lambda item: item[1], reverse=True)
    return [item[0] for item in selected[:MAX_MARKETS]]


def candles(symbol, bar="15m", limit=120):
    raw = api("/api/v5/market/candles", {
        "instId": symbol,
        "bar": bar,
        "limit": str(limit)
    })

    output = []
    for candle in reversed(raw):
        if len(candle) > 8 and candle[8] != "1":
            continue

        output.append({
            "ts": int(candle[0]),
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5])
        })
    return output



def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2.0 / (period + 1.0)
    value = sum(values[:period]) / period
    for price in values[period:]:
        value = alpha * price + (1.0 - alpha) * value
    return value


def btc_regime():
    """Return BULLISH, BEARISH or NEUTRAL using BTC 1H trend + 3H momentum."""
    data = candles(BTC_SYMBOL, BTC_BAR, 100)
    closes = [c["close"] for c in data]
    if len(closes) < BTC_SLOW_EMA + BTC_MOMENTUM_BARS + 2:
        return "NEUTRAL", "BTC Daten unzureichend"

    fast = ema(closes, BTC_FAST_EMA)
    slow = ema(closes, BTC_SLOW_EMA)
    current = closes[-1]
    previous = closes[-1 - BTC_MOMENTUM_BARS]
    momentum = (current / previous - 1.0) * 100.0 if previous else 0.0

    if current < fast < slow and momentum <= -BTC_STRONG_MOVE_PCT:
        return "BEARISH", f"BTC bearish: 3H {momentum:.2f}% | Close < EMA20 < EMA50"
    if current > fast > slow and momentum >= BTC_STRONG_MOVE_PCT:
        return "BULLISH", f"BTC bullish: 3H +{momentum:.2f}% | Close > EMA20 > EMA50"
    return "NEUTRAL", f"BTC neutral: 3H {momentum:.2f}%"


def rsi_series(closes, period=14):
    if len(closes) < period + 2:
        return []

    values = [None] * len(closes)
    gains, losses = [], []

    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    values[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]
        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

        values[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)

    return values


def atr(data, period=14):
    if len(data) < 2:
        return 0

    true_ranges = []
    for i in range(1, len(data)):
        current = data[i]
        previous = data[i - 1]

        true_ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"])
        ))

    values = true_ranges[-period:]
    return sum(values) / len(values) if values else 0


def body(c):
    return abs(c["close"] - c["open"])


def candle_range(c):
    return max(c["high"] - c["low"], 1e-12)


def upper_wick(c):
    return c["high"] - max(c["open"], c["close"])


def lower_wick(c):
    return min(c["open"], c["close"]) - c["low"]


def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


def bullish_hammer(c):
    # Fibonacci 0-0.382: entire candle body in upper 38.2%.
    rng = candle_range(c)
    return (min(c["open"], c["close"]) >= c["high"] - 0.382 * rng
            and lower_wick(c) > 0)


def bearish_pinbar(c):
    # Mirrored Fibonacci: entire body in lower 38.2%.
    rng = candle_range(c)
    return (max(c["open"], c["close"]) <= c["low"] + 0.382 * rng
            and upper_wick(c) > 0)


def bullish_engulfing(previous, current):
    return (
        bearish(previous)
        and bullish(current)
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bearish_engulfing(previous, current):
    return (
        bullish(previous)
        and bearish(current)
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


def volume_confirmation(data):
    if len(data) < 22:
        return False

    previous = data[-21:-1]
    avg = sum(c["volume"] for c in previous) / len(previous)

    return avg > 0 and data[-1]["volume"] >= avg * 1.20


def rsi_recovery(rsis, side, lookback=10):
    valid = [value for value in rsis if value is not None]

    if len(valid) < lookback + 1:
        return None

    current = valid[-1]
    history = valid[-(lookback + 1):-1]

    if side == "LONG":
        extreme = min(history)
        if extreme <= RSI_OVERSOLD and current > RSI_LONG_RECOVERY:
            return {"extreme": extreme, "current": current}
    else:
        extreme = max(history)
        if extreme >= RSI_OVERBOUGHT and current < RSI_SHORT_RECOVERY:
            return {"extreme": extreme, "current": current}

    return None


def recent_reversal(data, side):
    if len(data) < 5:
        return None

    for i in range(len(data) - 3, len(data)):
        current = data[i]
        previous = data[i - 1]

        if side == "LONG":
            if bullish_engulfing(previous, current):
                return "Bullish Engulfing"
            if bullish_hammer(current):
                return "Bullish Hammer"
        else:
            if bearish_engulfing(previous, current):
                return "Bearish Engulfing"
            if bearish_pinbar(current):
                return "Bearish Pinbar"

    return None


def structure_break(data, side):
    if len(data) < 5:
        return False

    current = data[-1]
    previous = data[-4:-1]

    if side == "LONG":
        return current["close"] > max(c["high"] for c in previous)

    return current["close"] < min(c["low"] for c in previous)


def find_setup(data):
    if len(data) < 50:
        return None

    rsis = rsi_series([c["close"] for c in data], RSI_PERIOD)
    if not rsis:
        return None

    for side in ("LONG", "SHORT"):
        recovery = rsi_recovery(rsis, side)
        if not recovery:
            continue

        reversal = recent_reversal(data, side)
        structure = structure_break(data, side)

        if not (reversal and structure):
            continue

        reasons = [
            f"RSI {recovery['extreme']:.1f} -> {recovery['current']:.1f}",
            reversal,
            "Bullish Strukturbruch" if side == "LONG" else "Bearish Strukturbruch"
        ]

        if volume_confirmation(data):
            reasons.append("Volumen bestätigt")

        return {
            "side": side,
            "trigger": " + ".join(reasons)
        }

    return None


def create_trade(symbol, setup, data):
    side = setup["side"]
    entry = data[-1]["close"]
    a = atr(data, 14)

    if a <= 0:
        return None

    recent = data[-10:]

    if side == "LONG":
        swing = min(c["low"] for c in recent)
        stop = swing - a * ATR_STOP_BUFFER
        risk = entry - stop
        if risk <= 0:
            return None
        stop_pct = risk / entry * 100.0
        if stop_pct < LONG_MIN_STOP_PCT:
            return None
        target = entry + risk * RR
    else:
        swing = max(c["high"] for c in recent)
        stop = swing + a * ATR_STOP_BUFFER
        risk = stop - entry
        if risk <= 0:
            return None
        target = entry - risk * RR

    timestamp = now()

    return {
        "time": timestamp,
        "strategy": STRATEGY,
        "symbol": symbol,
        "side": side,
        "timeframe": "15m",
        "setup_type": "RSI Recovery + Reversal + Structure",
        "entry": num(entry),
        "stop": num(stop),
        "tp1": "",
        "tp2": num(target),
        "tp3": "",
        "planned_rr": "1:2",
        "trigger": setup["trigger"],
        "status": "OPEN",
        "last_checked": timestamp
    }


def load():
    if not os.path.exists(JOURNAL):
        return []

    with open(JOURNAL, newline="", encoding="utf-8") as file:
        rows = list(csv.DictReader(file))

    for row in rows:
        if not row.get("strategy"):
            row["strategy"] = "CURRENT"

    return rows


def save(rows):
    # Preserve newer journal columns (e.g. V7/V8) instead of deleting them.
    fieldnames = list(FIELDS)

    for row in rows:
        for field in row.keys():
            if field not in fieldnames:
                fieldnames.append(field)

    with open(JOURNAL, "w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()

        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def v4_rows(rows):
    return [row for row in rows if row.get("strategy") == STRATEGY]


def duplicate(rows, symbol, side):
    return any(
        row.get("strategy") == STRATEGY
        and row.get("symbol") == symbol
        and row.get("side") == side
        and row.get("status") == "OPEN"
        for row in rows
    )


def stop_cooldown_active(rows, symbol):
    cutoff = datetime.now(timezone.utc).timestamp() - STOP_COOLDOWN_HOURS * 3600
    for row in reversed(rows):
        if row.get("strategy") != STRATEGY or row.get("symbol") != symbol:
            continue
        if row.get("status") != "STOP":
            continue
        stamp = row.get("last_checked") or row.get("time") or ""
        try:
            ts = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
            if ts >= cutoff:
                return True
        except Exception:
            pass
    return False


def update_v4_journal(rows):
    changed = 0

    for row in rows:
        if row.get("strategy") != STRATEGY or row.get("status") != "OPEN":
            continue

        try:
            checkpoint = row.get("last_checked", "") or row["time"]
            checkpoint_ms = int(
                datetime.fromisoformat(checkpoint.replace("Z", "+00:00")).timestamp() * 1000
            )

            data = candles(row["symbol"], "15m", 100)
            relevant = [c for c in data if c["ts"] > checkpoint_ms]

            if not relevant:
                continue

            stop = flt(row["stop"])
            target = flt(row["tp2"])
            status = "OPEN"
            last_processed_ts = checkpoint_ms

            for candle in relevant:
                last_processed_ts = candle["ts"]

                if row["side"] == "LONG":
                    stop_hit = candle["low"] <= stop
                    target_hit = candle["high"] >= target
                else:
                    stop_hit = candle["high"] >= stop
                    target_hit = candle["low"] <= target

                if stop_hit and target_hit:
                    status = "UNCLEAR"
                    break
                if target_hit:
                    status = "TP2"
                    break
                if stop_hit:
                    status = "STOP"
                    break

            if status != row["status"]:
                print("V10 Journal:", row["symbol"], row["status"], "->", status)
                row["status"] = status
                changed += 1

            row["last_checked"] = datetime.fromtimestamp(
                last_processed_ts / 1000, tz=timezone.utc
            ).isoformat()

        except Exception as error:
            print("V10 Journal Fehler:", row.get("symbol"), error)

        time.sleep(PAUSE)

    return changed


def notify_new_setup(trade, trade_number):
    direction = "🟢 LONG" if trade["side"] == "LONG" else "🔴 SHORT"
    market_state = (
        "ÜBERVERKAUFT → UMKEHR"
        if trade["side"] == "LONG"
        else "ÜBERKAUFT → UMKEHR"
    )

    message = (
        "🚨 NEUES V10 MEAN-REVERSION SETUP\n\n"
        f"Trade: {trade_number}/{TEST_LIMIT}\n"
        f"Paar: {trade['symbol']}\n"
        f"Richtung: {direction}\n"
        f"Markt: {market_state}\n"
        f"Zeitrahmen: {trade['timeframe']}\n\n"
        f"Bestätigung:\n{trade['trigger']}\n\n"
        f"Entry: {trade['entry']}\n"
        f"Stop-Loss: {trade['stop']}\n"
        f"Take-Profit 2R: {trade['tp2']}\n\n"
        "Risk/Reward: 1:2"
    )

    telegram(message)


def coin_trend(symbol):
    """Independent 1H trend of each market, using closed candles."""
    data = candles(symbol, "1H", 100)
    closes = [c["close"] for c in data]
    if len(closes) < 52:
        return "NEUTRAL"
    fast = ema(closes, 20)
    slow = ema(closes, 50)
    if closes[-1] > fast > slow:
        return "BULLISH"
    if closes[-1] < fast < slow:
        return "BEARISH"
    return "NEUTRAL"


def scan_market(symbol, rows, regime):
    if stop_cooldown_active(rows, symbol):
        return None

    data = candles(symbol, "15m", 120)
    setup = find_setup(data)

    if not setup:
        return None

    trend = coin_trend(symbol)
    if setup["side"] == "LONG" and trend != "BULLISH":
        return None
    if setup["side"] == "SHORT" and trend != "BEARISH":
        return None

    # Do not fight a strong BTC regime.
    if regime == "BEARISH" and setup["side"] == "LONG":
        return None
    if regime == "BULLISH" and setup["side"] == "SHORT":
        return None

    if duplicate(rows, symbol, setup["side"]):
        return None

    return create_trade(symbol, setup, data)


def v4_stats(rows):
    trades = v4_rows(rows)
    wins = [r for r in trades if r.get("status") == "TP2"]
    losses = [r for r in trades if r.get("status") == "STOP"]
    unclear = [r for r in trades if r.get("status") == "UNCLEAR"]
    open_trades = [r for r in trades if r.get("status") == "OPEN"]

    completed = len(wins) + len(losses)
    winrate = len(wins) / completed * 100 if completed else 0

    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "unclear": len(unclear),
        "open": len(open_trades),
        "completed": completed,
        "winrate": winrate
    }


def main():
    print("V10 Mean Reversion Scanner gestartet")

    rows = load()
    updates = update_v4_journal(rows)

    current_count = len(v4_rows(rows))
    print("V10 Signale bisher:", current_count, "/", TEST_LIMIT)

    if current_count >= TEST_LIMIT:
        print("V10 Testlimit erreicht. Keine neuen V10 Entries.")
        save(rows)
        result = v4_stats(rows)
        print("V10 offen:", result["open"])
        print("V10 2R Gewinner:", result["wins"])
        print("V10 Stops:", result["losses"])
        print("V10 Winrate:", f"{result['winrate']:.1f}%")
        return

    regime, regime_reason = btc_regime()
    print("BTC Regime:", regime, "-", regime_reason)

    market_list = markets()
    scanned = errors = new_signals = 0

    for index, symbol in enumerate(market_list, 1):
        if len(v4_rows(rows)) >= TEST_LIMIT:
            break

        try:
            trade = scan_market(symbol, rows, regime)
            scanned += 1

            if trade:
                rows.append(trade)
                new_signals += 1
                trade_number = len(v4_rows(rows))

                print(
                    "NEW V10:", trade_number, "/", TEST_LIMIT,
                    symbol, trade["side"], trade["trigger"]
                )
                notify_new_setup(trade, trade_number)

        except Exception as error:
            errors += 1
            print("SCAN ERROR:", symbol, error)

        if index % 25 == 0:
            print("Progress:", index, "/", len(market_list))

        time.sleep(PAUSE)

    save(rows)
    result = v4_stats(rows)

    print("--------------------------------")
    print("V10 SCAN BEENDET")
    print("Märkte geprüft:", scanned)
    print("Fehler:", errors)
    print("Neue V10 Signale:", new_signals)
    print("V10 Journal Updates:", updates)
    print("V10 Trades gesamt:", result["total"])
    print("V10 offen:", result["open"])
    print("V10 Gewinner 2R:", result["wins"])
    print("V10 Stops:", result["losses"])
    print("V10 Unklar:", result["unclear"])
    print("V10 Winrate:", f"{result['winrate']:.1f}%")
    print("--------------------------------")


if __name__ == "__main__":
    main()
