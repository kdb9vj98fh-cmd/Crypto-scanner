import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# V3 MEAN REVERSION
# RSI-Extrem -> Umkehrbestätigung -> Entry -> SL -> TP 2R
# ============================================================

BASE = "https://www.okx.com"
JOURNAL = "signals.csv"

STRATEGY = "V3_MEAN_REVERSION"

MAX_MARKETS = 400
PAUSE = 0.035

RSI_PERIOD = 14
RSI_OVERSOLD = 25
RSI_OVERBOUGHT = 75

RR = 2.0
ATR_STOP_BUFFER = 0.15

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
    "last_checked"
]

TERMINAL = {
    "TP2",
    "STOP",
    "UNCLEAR"
}

# Bekannte Meme-Coins.
# Kann später erweitert werden.
MEME_BASES = {
    "DOGE",
    "SHIB",
    "PEPE",
    "BONK",
    "FLOKI",
    "WIF",
    "BOME",
    "MEME",
    "TURBO",
    "NEIRO",
    "BRETT",
    "MOG",
    "POPCAT",
    "MEW",
    "PONKE",
    "SLERF",
    "BABYDOGE",
    "DOGS",
    "CAT",
    "HIPPO",
    "PNUT",
    "GOAT",
    "ACT",
    "MOODENG",
    "TRUMP",
    "MELANIA"
}

STABLE_BASES = {
    "USDT",
    "USDC",
    "DAI",
    "FDUSD",
    "TUSD",
    "USDE",
    "PYUSD",
    "USDS",
    "BUSD",
    "USD0",
    "FRAX"
}


# ============================================================
# HELPERS
# ============================================================

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
#
# WICHTIG:
# Diese Funktion wird ausschließlich bei einem NEUEN
# bestätigten V3-Setup aufgerufen.
#
# Keine Statusmeldungen.
# Keine alten Strategien.
# Keine Startmeldung.
# Keine TP-/SL-Meldungen.
# ============================================================

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
            headers={
                "User-Agent": "CryptoScanner/V3"
            }
        )

        with urllib.request.urlopen(
            request,
            timeout=20
        ) as response:

            result = json.loads(
                response.read().decode()
            )

        if not result.get("ok"):
            raise RuntimeError(result)

        print(
            "Telegram: neues V3-Setup gesendet"
        )

        return True

    except Exception as error:

        print(
            "Telegram Fehler:",
            error
        )

        return False


# ============================================================
# OKX API
# ============================================================

def api(path, params=None):

    if params:
        path += "?" + urllib.parse.urlencode(
            params
        )

    request = urllib.request.Request(
        BASE + path,
        headers={
            "User-Agent":
                "Mozilla/5.0 CryptoScanner/V3"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=20
    ) as response:

        result = json.loads(
            response.read().decode()
        )

    if result.get("code") != "0":
        raise RuntimeError(result)

    return result.get("data", [])


# ============================================================
# MARKETS
# ============================================================

def markets():

    instruments = api(
        "/api/v5/public/instruments",
        {
            "instType": "SWAP"
        }
    )

    tickers = api(
        "/api/v5/market/tickers",
        {
            "instType": "SWAP"
        }
    )

    volumes = {
        ticker.get("instId", ""):
            flt(ticker.get("volCcy24h"))
        for ticker in tickers
    }

    selected = []

    for instrument in instruments:

        symbol = instrument.get(
            "instId",
            ""
        )

        if not symbol.endswith(
            "-USDT-SWAP"
        ):
            continue

        if instrument.get(
            "state"
        ) != "live":
            continue

        base = symbol.split("-")[0]

        if base in STABLE_BASES:
            continue

        if base in MEME_BASES:
            continue

        volume = volumes.get(
            symbol,
            0
        )

        # Märkte ohne brauchbares Volumen ignorieren
        if volume <= 0:
            continue

        selected.append(
            (
                symbol,
                volume
            )
        )

    # Liquideste zuerst
    selected.sort(
        key=lambda item: item[1],
        reverse=True
    )

    return [
        item[0]
        for item in selected[
            :MAX_MARKETS
        ]
    ]


# ============================================================
# CANDLES
# ============================================================

def candles(
    symbol,
    bar="15m",
    limit=120
):

    raw = api(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": bar,
            "limit": str(limit)
        }
    )

    output = []

    for candle in reversed(raw):

        # Nur abgeschlossene Kerzen
        if (
            len(candle) > 8
            and candle[8] != "1"
        ):
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


# ============================================================
# RSI
# ============================================================

def rsi_series(
    closes,
    period=14
):

    if len(closes) < period + 2:
        return []

    values = [
        None
    ] * len(closes)

    gains = []
    losses = []

    for i in range(
        1,
        period + 1
    ):

        change = (
            closes[i]
            - closes[i - 1]
        )

        gains.append(
            max(
                change,
                0
            )
        )

        losses.append(
            max(
                -change,
                0
            )
        )

    avg_gain = (
        sum(gains)
        / period
    )

    avg_loss = (
        sum(losses)
        / period
    )

    if avg_loss == 0:
        values[period] = 100.0

    else:

        rs = (
            avg_gain
            / avg_loss
        )

        values[period] = (
            100
            - 100 / (
                1 + rs
            )
        )

    for i in range(
        period + 1,
        len(closes)
    ):

        change = (
            closes[i]
            - closes[i - 1]
        )

        gain = max(
            change,
            0
        )

        loss = max(
            -change,
            0
        )

        avg_gain = (
            avg_gain
            * (period - 1)
            + gain
        ) / period

        avg_loss = (
            avg_loss
            * (period - 1)
            + loss
        ) / period

        if avg_loss == 0:

            values[i] = 100.0

        else:

            rs = (
                avg_gain
                / avg_loss
            )

            values[i] = (
                100
                - 100 / (
                    1 + rs
                )
            )

    return values


# ============================================================
# ATR
# ============================================================

def atr(
    data,
    period=14
):

    if len(data) < 2:
        return 0

    true_ranges = []

    for i in range(
        1,
        len(data)
    ):

        current = data[i]
        previous = data[i - 1]

        true_range = max(

            current["high"]
            - current["low"],

            abs(
                current["high"]
                - previous["close"]
            ),

            abs(
                current["low"]
                - previous["close"]
            )
        )

        true_ranges.append(
            true_range
        )

    values = true_ranges[
        -period:
    ]

    if not values:
        return 0

    return (
        sum(values)
        / len(values)
    )


# ============================================================
# CANDLE HELPERS
# ============================================================

def body(candle):

    return abs(
        candle["close"]
        - candle["open"]
    )


def candle_range(candle):

    return max(
        candle["high"]
        - candle["low"],
        1e-12
    )


def upper_wick(candle):

    return (
        candle["high"]
        - max(
            candle["open"],
            candle["close"]
        )
    )


def lower_wick(candle):

    return (
        min(
            candle["open"],
            candle["close"]
        )
        - candle["low"]
    )


def bullish(candle):

    return (
        candle["close"]
        > candle["open"]
    )


def bearish(candle):

    return (
        candle["close"]
        < candle["open"]
    )


# ============================================================
# HAMMER / PINBAR
# ============================================================

def bullish_hammer(candle):

    rng = candle_range(
        candle
    )

    b = body(
        candle
    )

    return (
        lower_wick(candle)
        >= max(
            b * 2,
            rng * 0.40
        )
        and
        upper_wick(candle)
        <= rng * 0.25
        and
        candle["close"]
        >= candle["low"]
        + rng * 0.60
    )


def bearish_hammer(candle):

    rng = candle_range(
        candle
    )

    b = body(
        candle
    )

    return (
        upper_wick(candle)
        >= max(
            b * 2,
            rng * 0.40
        )
        and
        lower_wick(candle)
        <= rng * 0.25
        and
        candle["close"]
        <= candle["low"]
        + rng * 0.40
    )


# ============================================================
# ENGULFING
# ============================================================

def bullish_engulfing(
    previous,
    current
):

    return (
        bearish(previous)
        and bullish(current)
        and
        current["open"]
        <= previous["close"]
        and
        current["close"]
        >= previous["open"]
    )


def bearish_engulfing(
    previous,
    current
):

    return (
        bullish(previous)
        and bearish(current)
        and
        current["open"]
        >= previous["close"]
        and
        current["close"]
        <= previous["open"]
    )


# ============================================================
# VOLUME
# ============================================================

def average_volume(
    data,
    period=20
):

    values = [
        candle["volume"]
        for candle in data[
            -period:
        ]
    ]

    if not values:
        return 0

    return (
        sum(values)
        / len(values)
    )


def volume_confirmation(
    data
):

    if len(data) < 22:
        return False

    current = data[-1]

    avg = average_volume(
        data[:-1],
        20
    )

    if avg <= 0:
        return False

    return (
        current["volume"]
        >= avg * 1.20
    )


# ============================================================
# CLOSE ABOVE / BELOW PREVIOUS CANDLE
# ============================================================

def close_above_previous(
    previous,
    current
):

    return (
        bullish(current)
        and
        current["close"]
        > previous["high"]
    )


def close_below_previous(
    previous,
    current
):

    return (
        bearish(current)
        and
        current["close"]
        < previous["low"]
    )


# ============================================================
# FIND EXTREME RSI
#
# Wir schauen nicht nur auf die aktuelle Kerze.
#
# Beispiel LONG:
# RSI war innerhalb der letzten Kerzen <=25.
# Danach warten wir auf Umkehrbestätigung.
# ============================================================

def recent_extreme(
    rsi_values,
    side,
    lookback=8
):

    recent = [
        value
        for value in rsi_values[
            -lookback:
        ]
        if value is not None
    ]

    if not recent:
        return None

    if side == "LONG":

        extreme = min(
            recent
        )

        if extreme <= RSI_OVERSOLD:
            return extreme

    else:

        extreme = max(
            recent
        )

        if extreme >= RSI_OVERBOUGHT:
            return extreme

    return None


# ============================================================
# V3 SETUP
# ============================================================

def find_setup(data):

    if len(data) < 40:
        return None

    current = data[-1]
    previous = data[-2]

    closes = [
        candle["close"]
        for candle in data
    ]

    rsis = rsi_series(
        closes,
        RSI_PERIOD
    )

    # --------------------------------------------------------
    # LONG:
    # Markt war extrem überverkauft.
    # Danach bullish confirmation.
    # --------------------------------------------------------

    long_rsi = recent_extreme(
        rsis,
        "LONG"
    )

    if long_rsi is not None:

        hammer = bullish_hammer(
            current
        )

        engulfing = (
            bullish_engulfing(
                previous,
                current
            )
        )

        close_confirm = (
            close_above_previous(
                previous,
                current
            )
        )

        volume = (
            volume_confirmation(
                data
            )
        )

        # Mindestens ein echter Price-Action Trigger.
        price_confirmation = (
            hammer
            or engulfing
            or close_confirm
        )

        if price_confirmation:

            reasons = []

            if hammer:
                reasons.append(
                    "Bullish Hammer"
                )

            if engulfing:
                reasons.append(
                    "Bullish Engulfing"
                )

            if close_confirm:
                reasons.append(
                    "Close über vorherigem Hoch"
                )

            if volume:
                reasons.append(
                    "Volumen bestätigt"
                )

            return {
                "side": "LONG",
                "extreme_rsi": long_rsi,
                "trigger":
                    " + ".join(
                        reasons
                    )
            }

    # --------------------------------------------------------
    # SHORT:
    # Markt war extrem überkauft.
    # Danach bearish confirmation.
    # --------------------------------------------------------

    short_rsi = recent_extreme(
        rsis,
        "SHORT"
    )

    if short_rsi is not None:

        hammer = bearish_hammer(
            current
        )

        engulfing = (
            bearish_engulfing(
                previous,
                current
            )
        )

        close_confirm = (
            close_below_previous(
                previous,
                current
            )
        )

        volume = (
            volume_confirmation(
                data
            )
        )

        price_confirmation = (
            hammer
            or engulfing
            or close_confirm
        )

        if price_confirmation:

            reasons = []

            if hammer:
                reasons.append(
                    "Bearish Pinbar"
                )

            if engulfing:
                reasons.append(
                    "Bearish Engulfing"
                )

            if close_confirm:
                reasons.append(
                    "Close unter vorherigem Tief"
                )

            if volume:
                reasons.append(
                    "Volumen bestätigt"
                )

            return {
                "side": "SHORT",
                "extreme_rsi": short_rsi,
                "trigger":
                    " + ".join(
                        reasons
                    )
            }

    return None


# ============================================================
# CREATE TRADE
# ============================================================

def create_trade(
    symbol,
    setup,
    data
):

    side = setup["side"]

    entry = data[-1]["close"]

    a = atr(
        data,
        14
    )

    if a <= 0:
        return None

    # Lokale Struktur
    recent = data[-8:]

    if side == "LONG":

        swing = min(
            candle["low"]
            for candle in recent
        )

        stop = (
            swing
            - a * ATR_STOP_BUFFER
        )

        risk = (
            entry
            - stop
        )

        if risk <= 0:
            return None

        target = (
            entry
            + risk * RR
        )

    else:

        swing = max(
            candle["high"]
            for candle in recent
        )

        stop = (
            swing
            + a * ATR_STOP_BUFFER
        )

        risk = (
            stop
            - entry
        )

        if risk <= 0:
            return None

        target = (
            entry
            - risk * RR
        )

    timestamp = now()

    return {

        "time":
            timestamp,

        "strategy":
            STRATEGY,

        "symbol":
            symbol,

        "side":
            side,

        "timeframe":
            "15m",

        "setup_type":
            "RSI Extreme Mean Reversion",

        "entry":
            num(entry),

        "stop":
            num(stop),

        # TP1 bleibt leer.
        "tp1":
            "",

        # Unser einziges Ziel:
        # TP = 2R
        "tp2":
            num(target),

        "tp3":
            "",

        "planned_rr":
            "1:2",

        "trigger":
            (
                f"RSI Extrem "
                f"{setup['extreme_rsi']:.1f}"
                f" | "
                f"{setup['trigger']}"
            ),

        "status":
            "OPEN",

        "last_checked":
            timestamp
    }


# ============================================================
# JOURNAL
# ============================================================

def load():

    if not os.path.exists(
        JOURNAL
    ):
        return []

    with open(
        JOURNAL,
        newline="",
        encoding="utf-8"
    ) as file:

        rows = list(
            csv.DictReader(
                file
            )
        )

    # Alte Daten bleiben erhalten.
    for row in rows:

        if not row.get(
            "strategy"
        ):
            row[
                "strategy"
            ] = "CURRENT"

    return rows


def save(rows):

    with open(
        JOURNAL,
        "w",
        newline="",
        encoding="utf-8"
    ) as file:

        writer = csv.DictWriter(
            file,
            fieldnames=FIELDS
        )

        writer.writeheader()

        for row in rows:

            writer.writerow({
                field:
                    row.get(
                        field,
                        ""
                    )
                for field
                in FIELDS
            })


# ============================================================
# DUPLICATE
#
# Nur V3 zählt.
# Alte Strategien sind irrelevant.
# ============================================================

def duplicate(
    rows,
    symbol,
    side
):

    return any(

        row.get(
            "strategy"
        ) == STRATEGY

        and

        row.get(
            "symbol"
        ) == symbol

        and

        row.get(
            "side"
        ) == side

        and

        row.get(
            "status"
        ) == "OPEN"

        for row in rows
    )


# ============================================================
# UPDATE V3 JOURNAL
#
# EXTREM WICHTIG:
#
# Hier wird KEIN Telegram aufgerufen.
#
# Alte Strategien werden komplett ignoriert.
#
# Status wird nur still im CSV aktualisiert.
# ============================================================

def update_v3_journal(rows):

    changed = 0

    for row in rows:

        # Alte Scanner komplett ignorieren.
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

            signal_time = int(

                datetime.fromisoformat(

                    row["time"].replace(
                        "Z",
                        "+00:00"
                    )

                ).timestamp()

                * 1000
            )

            data = candles(
                row["symbol"],
                "15m",
                100
            )

            relevant = [

                candle

                for candle in data

                if candle["ts"]
                > signal_time
            ]

            stop = flt(
                row["stop"]
            )

            target = flt(
                row["tp2"]
            )

            status = "OPEN"

            for candle in relevant:

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

                # Gleiche Kerze:
                # Reihenfolge unbekannt.
                if (
                    stop_hit
                    and target_hit
                ):

                    status = (
                        "UNCLEAR"
                    )

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
                    "V3 Update:",
                    row["symbol"],
                    row["status"],
                    "->",
                    status
                )

                row["status"] = (
                    status
                )

                changed += 1

            row[
                "last_checked"
            ] = now()

        except Exception as error:

            print(
                "Journal Fehler:",
                row.get(
                    "symbol"
                ),
                error
            )

        time.sleep(
            PAUSE
        )

    return changed


# ============================================================
# TELEGRAM:
# NUR NEUES SETUP
# ============================================================

def notify_new_setup(
    trade
):

    if (
        trade["side"]
        == "LONG"
    ):

        direction = (
            "🟢 LONG"
        )

        market_state = (
            "ÜBERVERKAUFT"
        )

    else:

        direction = (
            "🔴 SHORT"
        )

        market_state = (
            "ÜBERKAUFT"
        )

    message = (

        "🚨 NEUES V3 MEAN-REVERSION SETUP\n\n"

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

        f"Stop-Loss: "
        f"{trade['stop']}\n"

        f"Take-Profit: "
        f"{trade['tp2']}\n\n"

        "Risk/Reward: 1:2"
    )

    # EINZIGER Telegram-Aufruf
    # für den normalen Scanner.
    telegram(
        message
    )


# ============================================================
# SCAN ONE MARKET
# ============================================================

def scan_market(
    symbol,
    rows
):

    data = candles(
        symbol,
        "15m",
        120
    )

    setup = find_setup(
        data
    )

    if not setup:
        return None

    if duplicate(
        rows,
        symbol,
        setup["side"]
    ):
        return None

    return create_trade(
        symbol,
        setup,
        data
    )


# ============================================================
# STATS
# ============================================================

def v3_stats(rows):

    trades = [

        row

        for row in rows

        if row.get(
            "strategy"
        ) == STRATEGY
    ]

    wins = [

        row

        for row in trades

        if row.get(
            "status"
        ) == "TP2"
    ]

    losses = [

        row

        for row in trades

        if row.get(
            "status"
        ) == "STOP"
    ]

    unclear = [

        row

        for row in trades

        if row.get(
            "status"
        ) == "UNCLEAR"
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
        "total":
            len(trades),

        "wins":
            len(wins),

        "losses":
            len(losses),

        "unclear":
            len(unclear),

        "completed":
            completed,

        "winrate":
            winrate
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "V3 Mean Reversion Scanner gestartet"
    )

    rows = load()

    # Nur V3 wird still aktualisiert.
    # KEINE Telegram-Meldung.
    updates = update_v3_journal(
        rows
    )

    try:

        market_list = markets()

    except Exception as error:

        # KEIN Telegram.
        # Nur GitHub Actions Log.
        print(
            "MARKET LIST ERROR:",
            error
        )

        save(rows)

        raise

    print(
        "Geeignete Nicht-Meme/"
        "Nicht-Stablecoin-Märkte:",
        len(market_list)
    )

    scanned = 0
    errors = 0
    new_signals = 0

    for index, symbol in enumerate(
        market_list,
        1
    ):

        try:

            trade = scan_market(
                symbol,
                rows
            )

            scanned += 1

            if trade:

                rows.append(
                    trade
                )

                new_signals += 1

                print(
                    "NEW V3:",
                    symbol,
                    trade["side"],
                    trade["trigger"]
                )

                # Telegram wirklich nur hier.
                notify_new_setup(
                    trade
                )

        except Exception as error:

            errors += 1

            print(
                "SCAN ERROR:",
                symbol,
                error
            )

        if index % 25 == 0:

            print(
                "Progress:",
                index,
                "/",
                len(
                    market_list
                )
            )

        time.sleep(
            PAUSE
        )

    save(
        rows
    )

    stats = v3_stats(
        rows
    )

    print(
        "--------------------------------"
    )

    print(
        "V3 SCAN BEENDET"
    )

    print(
        "Märkte geprüft:",
        scanned
    )

    print(
        "Fehler:",
        errors
    )

    print(
        "Neue V3 Signale:",
        new_signals
    )

    print(
        "Journal Updates:",
        updates
    )

    print(
        "V3 Trades gesamt:",
        stats["total"]
    )

    print(
        "V3 Gewinner (2R):",
        stats["wins"]
    )

    print(
        "V3 Verlierer:",
        stats["losses"]
    )

    print(
        "V3 Unklar:",
        stats["unclear"]
    )

    print(
        "V3 Winrate:",
        f"{stats['winrate']:.1f}%"
    )

    print(
        "--------------------------------"
    )


if __name__ == "__main__":
    main()