import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE = "https://www.okx.com"
JOURNAL = "signals.csv"
STRATEGY = "V5_MEAN_REVERSION"

MAX_MARKETS = 400
TEST_LIMIT = 100
PAUSE = 0.02

API_TIMEOUT = 6
API_RETRIES = 1

RSI_PERIOD = 14
RSI_OVERSOLD = 25
RSI_OVERBOUGHT = 75
RSI_LONG_RECOVERY = 30
RSI_SHORT_RECOVERY = 70

ATR_PERIOD = 14
ATR_MULTIPLIER = 2.0
RR = 2.0

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FIELDS = [
    "time",
    "strategy",
    "symbol",
    "side",
    "timeframe",
    "setup_type",
    "entry",
    "stop",
    "tp1",
    "tp2",
    "tp3",
    "planned_rr",
    "trigger",
    "status",
    "last_checked",
]

MEME_BASES = {
    "DOGE", "SHIB", "PEPE", "BONK", "FLOKI", "WIF",
    "BOME", "MEME", "TURBO", "NEIRO", "BRETT", "MOG",
    "POPCAT", "MEW", "PONKE", "SLERF", "BABYDOGE",
    "DOGS", "CAT", "HIPPO", "PNUT", "GOAT", "ACT",
    "MOODENG", "TRUMP", "MELANIA",
}

STABLE_BASES = {
    "USDT", "USDC", "DAI", "FDUSD", "TUSD", "USDE",
    "PYUSD", "USDS", "BUSD", "USD0", "FRAX",
}

# Bekannte Nicht-Krypto / tokenisierte TradFi-Produkte
TRADFI_BASES = {
    "AAPL", "AMD", "AMZN", "APP", "COIN", "CRWV",
    "DKNG", "GOOGL", "H100", "IBM", "INTC", "INTW",
    "IREN", "META", "MRNA", "MSFT", "MSTU", "MU",
    "NKE", "NVDA", "ONDS", "ORCL", "PLTR", "SOFTBANK",
    "SPCX", "TSLA", "TSLL", "TSM", "VRT", "WDC",
    "XAG", "XAU", "AEHR", "GPRO",
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


# ============================================================
# TELEGRAM
# NUR neue V5-Signale.
# Keine TP/SL-, Status- oder Fehlermeldungen.
# ============================================================

def telegram(text):
    if not TOKEN or not CHAT_ID:
        print("Telegram Secrets fehlen")
        return False

    try:
        data = urllib.parse.urlencode({
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode()

        request = urllib.request.Request(
            f"https://api.telegram.org/bot{TOKEN}/sendMessage",
            data=data,
            headers={"User-Agent": "CryptoScanner/V5"},
        )

        with urllib.request.urlopen(
            request,
            timeout=8,
        ) as response:
            result = json.loads(response.read().decode())

        if not result.get("ok"):
            raise RuntimeError(result)

        print("Telegram: neues V5-Setup gesendet")
        return True

    except Exception as error:
        print("Telegram Fehler:", error)
        return False


# ============================================================
# OKX
# Kurzer Timeout + Retry, damit ein Request nicht den
# kompletten GitHub-Run blockiert.
# ============================================================

def api(path, params=None):
    if params:
        path += "?" + urllib.parse.urlencode(params)

    last_error = None

    for attempt in range(API_RETRIES + 1):
        try:
            request = urllib.request.Request(
                BASE + path,
                headers={
                    "User-Agent": "Mozilla/5.0 CryptoScanner/V5"
                },
            )

            with urllib.request.urlopen(
                request,
                timeout=API_TIMEOUT,
            ) as response:
                result = json.loads(response.read().decode())

            if result.get("code") != "0":
                raise RuntimeError(result)

            return result.get("data", [])

        except Exception as error:
            last_error = error

            if attempt < API_RETRIES:
                time.sleep(0.35)

    raise last_error


# ============================================================
# MÄRKTE
# ============================================================

def markets():
    instruments = api(
        "/api/v5/public/instruments",
        {"instType": "SWAP"},
    )

    tickers = api(
        "/api/v5/market/tickers",
        {"instType": "SWAP"},
    )

    volumes = {
        ticker.get("instId", ""):
        flt(ticker.get("volCcy24h"))
        for ticker in tickers
    }

    selected = []

    for instrument in instruments:
        symbol = instrument.get("instId", "")

        if not symbol.endswith("-USDT-SWAP"):
            continue

        if instrument.get("state") != "live":
            continue

        base = symbol.split("-")[0].upper()

        if base in STABLE_BASES:
            continue

        if base in MEME_BASES:
            continue

        if base in TRADFI_BASES:
            continue

        volume = volumes.get(symbol, 0)

        if volume <= 0:
            continue

        selected.append((symbol, volume))

    selected.sort(
        key=lambda item: item[1],
        reverse=True,
    )

    return [
        item[0]
        for item in selected[:MAX_MARKETS]
    ]


# ============================================================
# KERZEN
# ============================================================

def candles(symbol, bar="15m", limit=120):
    raw = api(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": bar,
            "limit": str(limit),
        },
    )

    output = []

    for candle in reversed(raw):

        # Nur abgeschlossene Kerzen
        if len(candle) > 8 and candle[8] != "1":
            continue

        output.append({
            "ts": int(candle[0]),
            "open": float(candle[1]),
            "high": float(candle[2]),
            "low": float(candle[3]),
            "close": float(candle[4]),
            "volume": float(candle[5]),
        })

    return output


# ============================================================
# RSI
# ============================================================

def rsi_series(closes, period=14):
    if len(closes) < period + 2:
        return []

    values = [None] * len(closes)

    gains = []
    losses = []

    for i in range(1, period + 1):
        change = closes[i] - closes[i - 1]

        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:
        values[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        values[period] = 100 - 100 / (1 + rs)

    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i - 1]

        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = (
            avg_gain * (period - 1) + gain
        ) / period

        avg_loss = (
            avg_loss * (period - 1) + loss
        ) / period

        if avg_loss == 0:
            values[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            values[i] = 100 - 100 / (1 + rs)

    return values


# ============================================================
# ATR
# ============================================================

def atr(data, period=14):
    if len(data) < period + 1:
        return 0

    true_ranges = []

    for i in range(1, len(data)):
        current = data[i]
        previous = data[i - 1]

        true_range = max(
            current["high"] - current["low"],
            abs(current["high"] - previous["close"]),
            abs(current["low"] - previous["close"]),
        )

        true_ranges.append(true_range)

    values = true_ranges[-period:]

    if not values:
        return 0

    return sum(values) / len(values)


# ============================================================
# CANDLE HELPERS
# ============================================================

def bullish(candle):
    return candle["close"] > candle["open"]


def bearish(candle):
    return candle["close"] < candle["open"]


def body(candle):
    return abs(
        candle["close"] - candle["open"]
    )


def candle_range(candle):
    return max(
        candle["high"] - candle["low"],
        1e-12,
    )


def upper_wick(candle):
    return (
        candle["high"]
        - max(
            candle["open"],
            candle["close"],
        )
    )


def lower_wick(candle):
    return (
        min(
            candle["open"],
            candle["close"],
        )
        - candle["low"]
    )


# ============================================================
# DEINE FIB-HAMMER-REGEL
#
# LONG:
# Preis ist gefallen.
# Fib wird vom LOW zum HIGH gemessen.
#
# High = 0
# Low = 1
#
# Der KOMPLETTE grüne Body muss im Bereich
# 0 bis 0.382 nahe dem HIGH liegen.
# ============================================================

def bullish_fib_hammer(candle):

    # Gegenfarbe Pflicht:
    # LONG-Bestätigung muss GRÜN sein.
    if not bullish(candle):
        return False

    rng = candle_range(candle)
    b = body(candle)

    fib382 = (
        candle["high"]
        - 0.382 * rng
    )

    body_inside_fib_zone = (
        min(
            candle["open"],
            candle["close"],
        )
        >= fib382
    )

    long_lower_wick = (
        lower_wick(candle)
        >= max(
            2 * b,
            rng * 0.40,
        )
    )

    small_upper_wick = (
        upper_wick(candle)
        <= rng * 0.25
    )

    return (
        body_inside_fib_zone
        and long_lower_wick
        and small_upper_wick
    )


# ============================================================
# DEINE FIB-PINBAR/HAMMER-REGEL
#
# SHORT:
# Preis ist gestiegen.
# Fib wird vom HIGH zum LOW gemessen.
#
# Der KOMPLETTE rote Body muss im Bereich
# 0 bis 0.382 nahe dem LOW liegen.
# ============================================================

def bearish_fib_pinbar(candle):

    # Gegenfarbe Pflicht:
    # SHORT-Bestätigung muss ROT sein.
    if not bearish(candle):
        return False

    rng = candle_range(candle)
    b = body(candle)

    fib382 = (
        candle["low"]
        + 0.382 * rng
    )

    body_inside_fib_zone = (
        max(
            candle["open"],
            candle["close"],
        )
        <= fib382
    )

    long_upper_wick = (
        upper_wick(candle)
        >= max(
            2 * b,
            rng * 0.40,
        )
    )

    small_lower_wick = (
        lower_wick(candle)
        <= rng * 0.25
    )

    return (
        body_inside_fib_zone
        and long_upper_wick
        and small_lower_wick
    )


# ============================================================
# ENGULFING
#
# Auch hier Gegenfarbe Pflicht:
#
# LONG  = grüne Bullish Engulfing
# SHORT = rote Bearish Engulfing
# ============================================================

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


# ============================================================
# RSI RECOVERY
# ============================================================

def rsi_recovery(rsis, side, lookback=10):

    valid = [
        value
        for value in rsis
        if value is not None
    ]

    if len(valid) < lookback + 1:
        return None

    current = valid[-1]
    history = valid[-(lookback + 1):-1]

    if side == "LONG":

        extreme = min(history)

        if (
            extreme <= RSI_OVERSOLD
            and current > RSI_LONG_RECOVERY
        ):
            return {
                "extreme": extreme,
                "current": current,
            }

    else:

        extreme = max(history)

        if (
            extreme >= RSI_OVERBOUGHT
            and current < RSI_SHORT_RECOVERY
        ):
            return {
                "extreme": extreme,
                "current": current,
            }

    return None


# ============================================================
# UMKEHRKERZE
# ============================================================

def recent_reversal(data, side):

    if len(data) < 5:
        return None

    # Umkehr darf in den letzten 3
    # abgeschlossenen Kerzen entstanden sein.
    for i in range(
        len(data) - 3,
        len(data),
    ):

        current = data[i]
        previous = data[i - 1]

        if side == "LONG":

            # ALLE Long-Bestätigungen grün
            if not bullish(current):
                continue

            if bullish_engulfing(
                previous,
                current,
            ):
                return "Bullish Engulfing"

            if bullish_fib_hammer(current):
                return "Bullish Fib-0.382 Hammer"

        else:

            # ALLE Short-Bestätigungen rot
            if not bearish(current):
                continue

            if bearish_engulfing(
                previous,
                current,
            ):
                return "Bearish Engulfing"

            if bearish_fib_pinbar(current):
                return "Bearish Fib-0.382 Pinbar"

    return None


# ============================================================
# STRUCTURE BREAK / CLOSE ABOVE / BELOW
#
# LONG:
# letzte Kerze MUSS grün sein und über den
# vorherigen 3 Hochs schließen.
#
# SHORT:
# letzte Kerze MUSS rot sein und unter den
# vorherigen 3 Tiefs schließen.
# ============================================================

def structure_break(data, side):

    if len(data) < 5:
        return False

    current = data[-1]
    previous = data[-4:-1]

    if side == "LONG":

        if not bullish(current):
            return False

        level = max(
            candle["high"]
            for candle in previous
        )

        return (
            current["close"] > level
        )

    if not bearish(current):
        return False

    level = min(
        candle["low"]
        for candle in previous
    )

    return (
        current["close"] < level
    )


# ============================================================
# VOLUMEN
# BONUS - KEIN MUSS
# ============================================================

def volume_confirmation(data):

    if len(data) < 22:
        return False

    previous = data[-21:-1]

    avg = (
        sum(
            candle["volume"]
            for candle in previous
        )
        / len(previous)
    )

    if avg <= 0:
        return False

    return (
        data[-1]["volume"]
        >= avg * 1.20
    )


# ============================================================
# FVG
#
# BONUS - KEIN MUSS.
#
# Wir suchen eine passende FVG-Zone und verlangen,
# dass die aktuelle Bestätigungskerze an dieser Zone
# reagiert.
# ============================================================

def fvg_confirmation(
    data,
    side,
    lookback=16,
):

    if len(data) < 5:
        return False

    current = data[-1]

    start = max(
        2,
        len(data) - lookback,
    )

    for i in range(
        start,
        len(data) - 1,
    ):

        first = data[i - 2]
        third = data[i]

        # Bullish FVG
        if side == "LONG":

            if (
                third["low"]
                > first["high"]
            ):

                zone_low = first["high"]
                zone_high = third["low"]

                reaction = (
                    current["low"]
                    <= zone_high
                    and
                    current["close"]
                    >= zone_low
                    and
                    bullish(current)
                )

                if reaction:
                    return True

        # Bearish FVG
        else:

            if (
                third["high"]
                < first["low"]
            ):

                zone_low = third["high"]
                zone_high = first["low"]

                reaction = (
                    current["high"]
                    >= zone_low
                    and
                    current["close"]
                    <= zone_high
                    and
                    bearish(current)
                )

                if reaction:
                    return True

    return False


# ============================================================
# ASCENDING / DESCENDING TRIANGLE
#
# BONUS - KEIN MUSS.
#
# Pattern wird NICHT vorhergesagt.
# Erst bestätigter Breakout zählt.
# ============================================================

def triangle_confirmation(
    data,
    side,
    lookback=20,
):

    if len(data) < lookback + 1:
        return False

    history = data[
        -(lookback + 1):-1
    ]

    current = data[-1]

    half = len(history) // 2

    first_half = history[:half]
    second_half = history[half:]

    if not first_half or not second_half:
        return False

    # ---------------- LONG ----------------

    if side == "LONG":

        first_resistance = max(
            candle["high"]
            for candle in first_half
        )

        second_resistance = max(
            candle["high"]
            for candle in second_half
        )

        tolerance = (
            max(
                first_resistance,
                second_resistance,
            )
            * 0.006
        )

        flat_resistance = (
            abs(
                second_resistance
                - first_resistance
            )
            <= tolerance
        )

        first_low = min(
            candle["low"]
            for candle in first_half
        )

        second_low = min(
            candle["low"]
            for candle in second_half
        )

        rising_lows = (
            second_low > first_low
        )

        resistance = max(
            candle["high"]
            for candle in history
        )

        breakout = (
            bullish(current)
            and
            current["close"]
            > resistance
        )

        return (
            flat_resistance
            and rising_lows
            and breakout
        )

    # ---------------- SHORT ----------------

    first_support = min(
        candle["low"]
        for candle in first_half
    )

    second_support = min(
        candle["low"]
        for candle in second_half
    )

    tolerance = (
        max(
            abs(first_support),
            abs(second_support),
        )
        * 0.006
    )

    flat_support = (
        abs(
            second_support
            - first_support
        )
        <= tolerance
    )

    first_high = max(
        candle["high"]
        for candle in first_half
    )

    second_high = max(
        candle["high"]
        for candle in second_half
    )

    falling_highs = (
        second_high < first_high
    )

    support = min(
        candle["low"]
        for candle in history
    )

    breakdown = (
        bearish(current)
        and
        current["close"]
        < support
    )

    return (
        flat_support
        and falling_highs
        and breakdown
    )


# ============================================================
# QUALITÄTS-BESTÄTIGUNGEN
#
# KEINE davon ist Pflicht.
# ============================================================

def quality_confirmations(data, side):

    extras = []

    if volume_confirmation(data):
        extras.append(
            "Volumen bestätigt"
        )

    if fvg_confirmation(
        data,
        side,
    ):
        extras.append(
            "Bullish FVG bestätigt"
            if side == "LONG"
            else "Bearish FVG bestätigt"
        )

    if triangle_confirmation(
        data,
        side,
    ):

        extras.append(
            "Ascending Triangle Breakout bestätigt"
            if side == "LONG"
            else
            "Descending Triangle Breakdown bestätigt"
        )

    return extras


# ============================================================
# V5 SETUP
# ============================================================

def find_setup(data):

    if len(data) < 50:
        return None

    closes = [
        candle["close"]
        for candle in data
    ]

    rsis = rsi_series(
        closes,
        RSI_PERIOD,
    )

    if not rsis:
        return None

    # ========================================================
    # LONG zuerst
    # ========================================================

    long_recovery = rsi_recovery(
        rsis,
        "LONG",
    )

    if long_recovery:

        reversal = recent_reversal(
            data,
            "LONG",
        )

        structure = structure_break(
            data,
            "LONG",
        )

        if reversal and structure:

            extras = quality_confirmations(
                data,
                "LONG",
            )

            reasons = [
                (
                    f"RSI "
                    f"{long_recovery['extreme']:.1f}"
                    f" -> "
                    f"{long_recovery['current']:.1f}"
                ),
                reversal,
                "Grüne Bestätigung",
                "Bullish Strukturbruch",
            ]

            reasons.extend(extras)

            return {
                "side": "LONG",
                "trigger":
                    " + ".join(reasons),
                "strong":
                    bool(extras),
            }

    # ========================================================
    # SHORT
    # ========================================================

    short_recovery = rsi_recovery(
        rsis,
        "SHORT",
    )

    if short_recovery:

        reversal = recent_reversal(
            data,
            "SHORT",
        )

        structure = structure_break(
            data,
            "SHORT",
        )

        if reversal and structure:

            extras = quality_confirmations(
                data,
                "SHORT",
            )

            reasons = [
                (
                    f"RSI "
                    f"{short_recovery['extreme']:.1f}"
                    f" -> "
                    f"{short_recovery['current']:.1f}"
                ),
                reversal,
                "Rote Bestätigung",
                "Bearish Strukturbruch",
            ]

            reasons.extend(extras)

            return {
                "side": "SHORT",
                "trigger":
                    " + ".join(reasons),
                "strong":
                    bool(extras),
            }

    return None


# ============================================================
# TRADE
#
# NEU:
# Stop = exakt 2 x ATR vom Entry.
# TP = exakt 2R.
# ============================================================

def create_trade(
    symbol,
    setup,
    data,
):

    side = setup["side"]

    entry = data[-1]["close"]

    current_atr = atr(
        data,
        ATR_PERIOD,
    )

    if current_atr <= 0:
        return None

    if side == "LONG":

        stop = (
            entry
            - ATR_MULTIPLIER
            * current_atr
        )

        risk = entry - stop

        target = (
            entry
            + risk * RR
        )

    else:

        stop = (
            entry
            + ATR_MULTIPLIER
            * current_atr
        )

        risk = stop - entry

        target = (
            entry
            - risk * RR
        )

    if risk <= 0:
        return None

    timestamp = now()

    setup_type = (
        "RSI Recovery + Reversal + Structure"
    )

    if setup["strong"]:
        setup_type += " + QUALITY"

    return {
        "time": timestamp,
        "strategy": STRATEGY,
        "symbol": symbol,
        "side": side,
        "timeframe": "15m",
        "setup_type": setup_type,
        "entry": num(entry),
        "stop": num(stop),
        "tp1": "",
        "tp2": num(target),
        "tp3": "",
        "planned_rr": "1:2",
        "trigger": setup["trigger"],
        "status": "OPEN",
        "last_checked": timestamp,
    }


# ============================================================
# JOURNAL
# Alte V4/V3/etc. Daten bleiben erhalten.
# ============================================================

def load():

    if not os.path.exists(JOURNAL):
        return []

    with open(
        JOURNAL,
        newline="",
        encoding="utf-8",
    ) as file:

        rows = list(
            csv.DictReader(file)
        )

    for row in rows:

        if not row.get("strategy"):
            row["strategy"] = "CURRENT"

    return rows


def save(rows):

    with open(
        JOURNAL,
        "w",
        newline="",
        encoding="utf-8",
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=FIELDS,
        )

        writer.writeheader()

        for row in rows:

            writer.writerow({
                field:
                row.get(field, "")
                for field in FIELDS
            })


def v5_rows(rows):

    return [
        row
        for row in rows
        if row.get("strategy")
        == STRATEGY
    ]


# ============================================================
# DUPLIKATE
# ============================================================

def duplicate(
    rows,
    symbol,
    side,
):

    return any(

        row.get("strategy")
        == STRATEGY

        and

        row.get("symbol")
        == symbol

        and

        row.get("side")
        == side

        and

        row.get("status")
        == "OPEN"

        for row in rows
    )


# ============================================================
# NUR V5 JOURNAL AKTUALISIEREN
#
# Kein Telegram bei TP/STOP.
# ============================================================

def update_v5_journal(rows):

    changed = 0

    for row in rows:

        if (
            row.get("strategy")
            != STRATEGY
        ):
            continue

        if (
            row.get("status")
            != "OPEN"
        ):
            continue

        try:

            checkpoint = (
                row.get(
                    "last_checked",
                    "",
                )
                or row["time"]
            )

            checkpoint_ms = int(
                datetime.fromisoformat(
                    checkpoint.replace(
                        "Z",
                        "+00:00",
                    )
                ).timestamp()
                * 1000
            )

            data = candles(
                row["symbol"],
                "15m",
                100,
            )

            relevant = [
                candle
                for candle in data
                if candle["ts"]
                > checkpoint_ms
            ]

            if not relevant:
                continue

            stop = flt(
                row["stop"]
            )

            target = flt(
                row["tp2"]
            )

            status = "OPEN"

            last_processed_ts = (
                checkpoint_ms
            )

            for candle in relevant:

                last_processed_ts = (
                    candle["ts"]
                )

                if (
                    row["side"]
                    == "LONG"
                ):

                    stop_hit = (
                        candle["low"]
                        <= stop
                    )

                    target_hit = (
                        candle["high"]
                        >= target
                    )

                else:

                    stop_hit = (
                        candle["high"]
                        >= stop
                    )

                    target_hit = (
                        candle["low"]
                        <= target
                    )

                if (
                    stop_hit
                    and target_hit
                ):

                    status = "UNCLEAR"
                    break

                if target_hit:

                    status = "TP2"
                    break

                if stop_hit:

                    status = "STOP"
                    break

            if (
                status
                != row["status"]
            ):

                print(
                    "V5 Journal:",
                    row["symbol"],
                    row["status"],
                    "->",
                    status,
                )

                row["status"] = status

                changed += 1

            row["last_checked"] = (
                datetime.fromtimestamp(
                    last_processed_ts / 1000,
                    tz=timezone.utc,
                ).isoformat()
            )

        except Exception as error:

            print(
                "V5 Journal Fehler:",
                row.get("symbol"),
                error,
            )

        time.sleep(PAUSE)

    return changed


# ============================================================
# TELEGRAM
# NUR HIER.
# ============================================================

def notify_new_setup(
    trade,
    trade_number,
):

    if trade["side"] == "LONG":

        direction = "🟢 LONG"
        market_state = (
            "ÜBERVERKAUFT → "
            "BULLISHE UMKEHR"
        )

    else:

        direction = "🔴 SHORT"
        market_state = (
            "ÜBERKAUFT → "
            "BEARISHE UMKEHR"
        )

    if (
        "+ QUALITY"
        in trade["setup_type"]
    ):

        quality = (
            "🔥 STARKES SIGNAL"
        )

    else:

        quality = (
            "STANDARD SIGNAL"
        )

    message = (
        "🚨 NEUES V5 MEAN-REVERSION SETUP\n\n"

        f"{quality}\n"

        f"Trade: "
        f"{trade_number}/{TEST_LIMIT}\n"

        f"Paar: "
        f"{trade['symbol']}\n"

        f"Richtung: "
        f"{direction}\n"

        f"Markt: "
        f"{market_state}\n"

        f"Zeitrahmen: "
        f"{trade['timeframe']}\n\n"

        f"Bestätigung:\n"
        f"{trade['trigger']}\n\n"

        f"Entry: "
        f"{trade['entry']}\n"

        f"Stop-Loss (2x ATR): "
        f"{trade['stop']}\n"

        f"Take-Profit 2R: "
        f"{trade['tp2']}\n\n"

        "Risk/Reward: 1:2"
    )

    telegram(message)


# ============================================================
# EINEN MARKT SCANNEN
# ============================================================

def scan_market(
    symbol,
    rows,
):

    data = candles(
        symbol,
        "15m",
        120,
    )

    setup = find_setup(data)

    if not setup:
        return None

    if duplicate(
        rows,
        symbol,
        setup["side"],
    ):
        return None

    return create_trade(
        symbol,
        setup,
        data,
    )


# ============================================================
# STATISTIK
# ============================================================

def stats(rows):

    trades = v5_rows(rows)

    wins = [
        row
        for row in trades
        if row.get("status")
        == "TP2"
    ]

    losses = [
        row
        for row in trades
        if row.get("status")
        == "STOP"
    ]

    unclear = [
        row
        for row in trades
        if row.get("status")
        == "UNCLEAR"
    ]

    open_trades = [
        row
        for row in trades
        if row.get("status")
        == "OPEN"
    ]

    completed = (
        len(wins)
        + len(losses)
    )

    winrate = (
        len(wins)
        / completed
        * 100
        if completed
        else 0
    )

    return {
        "total": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "unclear": len(unclear),
        "open": len(open_trades),
        "winrate": winrate,
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "V5 Mean Reversion Scanner gestartet"
    )

    rows = load()

    # Nur bestehende V5 Trades updaten.
    # V4 bleibt historisch unangetastet.
    updates = update_v5_journal(
        rows
    )

    current_count = len(
        v5_rows(rows)
    )

    print(
        "V5 Signale bisher:",
        current_count,
        "/",
        TEST_LIMIT,
    )

    # ========================================================
    # TESTLIMIT
    # ========================================================

    if current_count >= TEST_LIMIT:

        print(
            "V5 Testlimit erreicht."
        )

        print(
            "Keine neuen V5 Entries."
        )

        save(rows)

        result = stats(rows)

        print(
            "V5 offen:",
            result["open"],
        )

        print(
            "V5 Gewinner 2R:",
            result["wins"],
        )

        print(
            "V5 Stops:",
            result["losses"],
        )

        print(
            "V5 Winrate:",
            f"{result['winrate']:.1f}%",
        )

        return

    # ========================================================
    # MÄRKTE
    # ========================================================

    try:

        market_list = markets()

    except Exception as error:

        print(
            "MARKET LIST ERROR:",
            error,
        )

        save(rows)

        raise

    print(
        "Geeignete Crypto-Märkte:",
        len(market_list),
    )

    scanned = 0
    errors = 0
    new_signals = 0

    # ========================================================
    # SCAN
    # ========================================================

    for index, symbol in enumerate(
        market_list,
        1,
    ):

        if (
            len(v5_rows(rows))
            >= TEST_LIMIT
        ):

            print(
                "100 V5 Signale erreicht."
            )

            break

        try:

            trade = scan_market(
                symbol,
                rows,
            )

            scanned += 1

            if trade:

                rows.append(trade)

                new_signals += 1

                trade_number = len(
                    v5_rows(rows)
                )

                print(
                    "NEW V5:",
                    trade_number,
                    "/",
                    TEST_LIMIT,
                    symbol,
                    trade["side"],
                    trade["trigger"],
                )

                # Telegram ausschließlich
                # bei neuem V5-Signal.
                notify_new_setup(
                    trade,
                    trade_number,
                )

        except Exception as error:

            errors += 1

            print(
                "SCAN ERROR:",
                symbol,
                error,
            )

        if index % 25 == 0:

            print(
                "Progress:",
                index,
                "/",
                len(market_list),
            )

        time.sleep(PAUSE)

    # ========================================================
    # SPEICHERN
    # ========================================================

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

    print(
        "V5 Winrate:",
        f"{result['winrate']:.1f}%",
    )

    print("--------------------------------")


if __name__ == "__main__":
    main()