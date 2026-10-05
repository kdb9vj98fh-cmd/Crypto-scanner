import csv
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# V4 MEAN REVERSION
#
# RSI Extrem
# -> RSI verlässt Extremzone
# -> Hammer/Pinbar oder Engulfing
# -> lokaler Strukturbruch
# -> Entry
# -> struktureller Stop
# -> Take Profit 2R
# ============================================================

BASE = "https://www.okx.com"
JOURNAL = "signals.csv"

STRATEGY = "V4_MEAN_REVERSION"

MAX_MARKETS = 400
TEST_LIMIT = 100
PAUSE = 0.035

RSI_PERIOD = 14

RSI_OVERSOLD = 25
RSI_OVERBOUGHT = 75

# RSI muss sich wieder aus dem Extrem lösen
RSI_LONG_RECOVERY = 30
RSI_SHORT_RECOVERY = 70

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

# ============================================================
# AUSSCHLÜSSE
# ============================================================

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
# telegram() wird ausschließlich von notify_new_setup()
# aufgerufen.
#
# Keine V3 Meldungen.
# Keine Status Updates.
# Keine TP/SL Meldungen.
# Keine Startmeldungen.
# Keine Fehlermeldungen per Telegram.
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
                "User-Agent": "CryptoScanner/V4"
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

        print("Telegram: neues V4-Setup gesendet")

        return True

    except Exception as error:

        print("Telegram Fehler:", error)

        return False


# ============================================================
# OKX API
# ============================================================

def api(path, params=None):

    if params:
        path += "?" + urllib.parse.urlencode(params)

    request = urllib.request.Request(
        BASE + path,
        headers={
            "User-Agent": "Mozilla/5.0 CryptoScanner/V4"
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

        symbol = instrument.get("instId", "")

        if not symbol.endswith("-USDT-SWAP"):
            continue

        if instrument.get("state") != "live":
            continue

        base = symbol.split("-")[0]

        if base in STABLE_BASES:
            continue

        if base in MEME_BASES:
            continue

        volume = volumes.get(symbol, 0)

        if volume <= 0:
            continue

        selected.append(
            (
                symbol,
                volume
            )
        )

    # Liquideste Märkte zuerst
    selected.sort(
        key=lambda item: item[1],
        reverse=True
    )

    return [
        item[0]
        for item in selected[:MAX_MARKETS]
    ]


# ============================================================
# CANDLES
# ============================================================

def candles(symbol, bar="15m", limit=120):

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

        gains.append(
            max(change, 0)
        )

        losses.append(
            max(-change, 0)
        )

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    if avg_loss == 0:

        values[period] = 100.0

    else:

        rs = avg_gain / avg_loss

        values[period] = (
            100
            - 100 / (1 + rs)
        )

    for i in range(
        period + 1,
        len(closes)
    ):

        change = (
            closes[i]
            - closes[i - 1]
        )

        gain = max(change, 0)
        loss = max(-change, 0)

        avg_gain = (
            avg_gain * (period - 1)
            + gain
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + loss
        ) / period

        if avg_loss == 0:

            values[i] = 100.0

        else:

            rs = avg_gain / avg_loss

            values[i] = (
                100
                - 100 / (1 + rs)
            )

    return values


# ============================================================
# ATR
# ============================================================

def atr(data, period=14):

    if len(data) < 2:
        return 0

    true_ranges = []

    for i in range(1, len(data)):

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

        true_ranges.append(true_range)

    values = true_ranges[-period:]

    if not values:
        return 0

    return sum(values) / len(values)


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

    rng = candle_range(candle)
    b = body(candle)

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
        >= candle["low"] + rng * 0.60
    )


def bearish_pinbar(candle):

    rng = candle_range(candle)
    b = body(candle)

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
        <= candle["low"] + rng * 0.40
    )


# ============================================================
# ENGULFING
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
# VOLUME
#
# Nur Zusatzinformation.
# Volumen allein erzeugt KEIN Setup.
# ============================================================

def volume_confirmation(data):

    if len(data) < 22:
        return False

    previous = data[-21:-1]

    if not previous:
        return False

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
# RSI EXTREM + RECOVERY
#
# LONG:
# RSI muss vorher <=25 gewesen sein.
# Jetzt muss RSI wieder über 30 liegen.
#
# SHORT:
# RSI muss vorher >=75 gewesen sein.
# Jetzt muss RSI wieder unter 70 liegen.
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

    if not history:
        return None

    if side == "LONG":

        extreme = min(history)

        if (
            extreme <= RSI_OVERSOLD
            and current > RSI_LONG_RECOVERY
        ):

            return {
                "extreme": extreme,
                "current": current
            }

    else:

        extreme = max(history)

        if (
            extreme >= RSI_OVERBOUGHT
            and current < RSI_SHORT_RECOVERY
        ):

            return {
                "extreme": extreme,
                "current": current
            }

    return None


# ============================================================
# REVERSAL CANDLE
#
# Wir akzeptieren eine echte Umkehrkerze innerhalb
# der letzten 3 abgeschlossenen Kerzen.
#
# Dadurch darf z.B. erst ein Hammer entstehen und
# die nächste Kerze den Strukturbruch bestätigen.
# ============================================================

def recent_reversal(data, side):

    if len(data) < 5:
        return None

    # letzte 3 Kerzen überprüfen
    start = len(data) - 3

    for i in range(
        start,
        len(data)
    ):

        current = data[i]
        previous = data[i - 1]

        if side == "LONG":

            if bullish_engulfing(
                previous,
                current
            ):

                return "Bullish Engulfing"

            if bullish_hammer(current):

                return "Bullish Hammer"

        else:

            if bearish_engulfing(
                previous,
                current
            ):

                return "Bearish Engulfing"

            if bearish_pinbar(current):

                return "Bearish Pinbar"

    return None


# ============================================================
# LOKALER STRUKTURBRUCH
#
# Das ist der entscheidende zusätzliche Filter gegenüber V3.
#
# LONG:
# aktueller Close über den Hochs der vorherigen 3 Kerzen.
#
# SHORT:
# aktueller Close unter den Tiefs der vorherigen 3 Kerzen.
# ============================================================

def structure_break(data, side):

    if len(data) < 5:
        return False

    current = data[-1]

    previous = data[-4:-1]

    if side == "LONG":

        level = max(
            candle["high"]
            for candle in previous
        )

        return (
            current["close"]
            > level
        )

    else:

        level = min(
            candle["low"]
            for candle in previous
        )

        return (
            current["close"]
            < level
        )


# ============================================================
# V4 SETUP
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
        RSI_PERIOD
    )

    if not rsis:
        return None

    # ========================================================
    # LONG
    # ========================================================

    long_recovery = rsi_recovery(
        rsis,
        "LONG"
    )

    if long_recovery:

        reversal = recent_reversal(
            data,
            "LONG"
        )

        structure = structure_break(
            data,
            "LONG"
        )

        if reversal and structure:

            reasons = [
                (
                    f"RSI {long_recovery['extreme']:.1f}"
                    f" -> {long_recovery['current']:.1f}"
                ),
                reversal,
                "Bullish Strukturbruch"
            ]

            if volume_confirmation(data):

                reasons.append(
                    "Volumen bestätigt"
                )

            return {
                "side": "LONG",
                "extreme_rsi":
                    long_recovery["extreme"],
                "current_rsi":
                    long_recovery["current"],
                "trigger":
                    " + ".join(reasons)
            }

    # ========================================================
    # SHORT
    # ========================================================

    short_recovery = rsi_recovery(
        rsis,
        "SHORT"
    )

    if short_recovery:

        reversal = recent_reversal(
            data,
            "SHORT"
        )

        structure = structure_break(
            data,
            "SHORT"
        )

        if reversal and structure:

            reasons = [
                (
                    f"RSI {short_recovery['extreme']:.1f}"
                    f" -> {short_recovery['current']:.1f}"
                ),
                reversal,
                "Bearish Strukturbruch"
            ]

            if volume_confirmation(data):

                reasons.append(
                    "Volumen bestätigt"
                )

            return {
                "side": "SHORT",
                "extreme_rsi":
                    short_recovery["extreme"],
                "current_rsi":
                    short_recovery["current"],
                "trigger":
                    " + ".join(reasons)
            }

    return None


# ============================================================
# TRADE ERSTELLEN
# ============================================================

def create_trade(symbol, setup, data):

    side = setup["side"]

    entry = data[-1]["close"]

    a = atr(
        data,
        14
    )

    if a <= 0:
        return None

    # Lokale Struktur für Stop
    recent = data[-10:]

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
        "time": timestamp,
        "strategy": STRATEGY,
        "symbol": symbol,
        "side": side,
        "timeframe": "15m",
        "setup_type":
            "RSI Recovery + Reversal + Structure",
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


# ============================================================
# JOURNAL
# ============================================================

def load():

    if not os.path.exists(JOURNAL):
        return []

    with open(
        JOURNAL,
        newline="",
        encoding="utf-8"
    ) as file:

        rows = list(
            csv.DictReader(file)
        )

    # Alte Daten niemals löschen
    for row in rows:

        if not row.get("strategy"):

            row["strategy"] = "CURRENT"

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
                row.get(field, "")
                for field in FIELDS
            })


# ============================================================
# V4 TRADES
# ============================================================

def v4_rows(rows):

    return [
        row
        for row in rows
        if row.get("strategy") == STRATEGY
    ]


# ============================================================
# DUPLIKATE
# ============================================================

def duplicate(rows, symbol, side):

    return any(

        row.get("strategy") == STRATEGY

        and

        row.get("symbol") == symbol

        and

        row.get("side") == side

        and

        row.get("status") == "OPEN"

        for row in rows
    )


# ============================================================
# JOURNAL UPDATE
#
# NUR V4.
#
# V3 / V2 / CURRENT / CREAMER werden NICHT mehr verarbeitet.
#
# KEIN Telegram bei TP oder Stop.
# ============================================================

def update_v4_journal(rows):

    changed = 0

    for row in rows:

        if row.get("strategy") != STRATEGY:
            continue

        if row.get("status") != "OPEN":
            continue

        try:

            # Letzten verarbeiteten Zeitpunkt verwenden.
            # Damit wird nicht bei jedem Run die komplette
            # Trade-Historie erneut abgespielt.

            checkpoint = row.get(
                "last_checked",
                ""
            )

            try:

                checkpoint_ms = int(
                    datetime.fromisoformat(
                        checkpoint.replace(
                            "Z",
                            "+00:00"
                        )
                    ).timestamp()
                    * 1000
                )

            except Exception:

                checkpoint_ms = int(
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
                if candle["ts"] > checkpoint_ms
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

            last_processed_ts = checkpoint_ms

            for candle in relevant:

                last_processed_ts = candle["ts"]

                if row["side"] == "LONG":

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

                # Beide in derselben 15m Kerze:
                # Reihenfolge nicht sicher feststellbar.
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

                print(
                    "V4 Journal:",
                    row["symbol"],
                    row["status"],
                    "->",
                    status
                )

                row["status"] = status

                changed += 1

            # Zeitpunkt der tatsächlich zuletzt
            # verarbeiteten Kerze speichern.

            row["last_checked"] = (
                datetime.fromtimestamp(
                    last_processed_ts / 1000,
                    tz=timezone.utc
                ).isoformat()
            )

        except Exception as error:

            print(
                "V4 Journal Fehler:",
                row.get("symbol"),
                error
            )

        time.sleep(PAUSE)

    return changed


# ============================================================
# TELEGRAM
#
# EINZIGER ORT FÜR V4 SIGNAL TELEGRAM
# ============================================================

def notify_new_setup(trade, trade_number):

    if trade["side"] == "LONG":

        direction = "🟢 LONG"
        market_state = "ÜBERVERKAUFT → UMKEHR"

    else:

        direction = "🔴 SHORT"
        market_state = "ÜBERKAUFT → UMKEHR"

    message = (
        "🚨 NEUES V4 MEAN-REVERSION SETUP\n\n"

        f"Trade: {trade_number}/{TEST_LIMIT}\n"

        f"Paar: {trade['symbol']}\n"

        f"Richtung: {direction}\n"

        f"Markt: {market_state}\n"

        f"Zeitrahmen: {trade['timeframe']}\n\n"

        f"Bestätigung:\n"
        f"{trade['trigger']}\n\n"

        f"Entry: {trade['entry']}\n"

        f"Stop-Loss: {trade['stop']}\n"

        f"Take-Profit 2R: {trade['tp2']}\n\n"

        "Risk/Reward: 1:2"
    )

    telegram(message)


# ============================================================
# EINEN MARKT SCANNEN
# ============================================================

def scan_market(symbol, rows):

    data = candles(
        symbol,
        "15m",
        120
    )

    setup = find_setup(data)

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
# V4 STATISTIK
# ============================================================

def v4_stats(rows):

    trades = v4_rows(rows)

    wins = [
        row
        for row in trades
        if row.get("status") == "TP2"
    ]

    losses = [
        row
        for row in trades
        if row.get("status") == "STOP"
    ]

    unclear = [
        row
        for row in trades
        if row.get("status") == "UNCLEAR"
    ]

    open_trades = [
        row
        for row in trades
        if row.get("status") == "OPEN"
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
        "completed": completed,
        "winrate": winrate
    }


# ============================================================
# MAIN
# ============================================================

def main():

    print(
        "V4 Mean Reversion Scanner gestartet"
    )

    rows = load()

    # Nur bereits existierende V4 Trades aktualisieren.
    # Alte V3 Trades werden nicht mehr verarbeitet.
    # Kein Telegram.

    updates = update_v4_journal(rows)

    current_v4_count = len(
        v4_rows(rows)
    )

    print(
        "V4 Signale bisher:",
        current_v4_count,
        "/",
        TEST_LIMIT
    )

    # ========================================================
    # 100 SIGNAL LIMIT
    # ========================================================

    if current_v4_count >= TEST_LIMIT:

        print(
            "V4 Testlimit erreicht."
        )

        print(
            "Keine neuen V4 Entries."
        )

        save(rows)

        stats = v4_stats(rows)

        print(
            "V4 offen:",
            stats["open"]
        )

        print(
            "V4 2R Gewinner:",
            stats["wins"]
        )

        print(
            "V4 Stops:",
            stats["losses"]
        )

        print(
            "V4 Winrate:",
            f"{stats['winrate']:.1f}%"
        )

        return

    # ========================================================
    # MARKTLISTE
    # ========================================================

    try:

        market_list = markets()

    except Exception as error:

        # Nur GitHub Log.
        # KEIN Telegram.

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

    # ========================================================
    # SCAN
    # ========================================================

    for index, symbol in enumerate(
        market_list,
        1
    ):

        # Exakt bei 100 stoppen
        if len(v4_rows(rows)) >= TEST_LIMIT:

            print(
                "100 V4 Signale erreicht."
            )

            break

        try:

            trade = scan_market(
                symbol,
                rows
            )

            scanned += 1

            if trade:

                rows.append(trade)

                new_signals += 1

                trade_number = len(
                    v4_rows(rows)
                )

                print(
                    "NEW V4:",
                    trade_number,
                    "/",
                    TEST_LIMIT,
                    symbol,
                    trade["side"],
                    trade["trigger"]
                )

                # Telegram ausschließlich hier
                notify_new_setup(
                    trade,
                    trade_number
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
                len(market_list)
            )

        time.sleep(PAUSE)

    # ========================================================
    # SPEICHERN
    # ========================================================

    save(rows)

    stats = v4_stats(rows)

    print("--------------------------------")
    print("V4 SCAN BEENDET")
    print("Märkte geprüft:", scanned)
    print("Fehler:", errors)
    print("Neue V4 Signale:", new_signals)
    print("V4 Journal Updates:", updates)
    print("V4 Trades gesamt:", stats["total"])
    print("V4 offen:", stats["open"])
    print("V4 Gewinner 2R:", stats["wins"])
    print("V4 Stops:", stats["losses"])
    print("V4 Unklar:", stats["unclear"])
    print(
        "V4 Winrate:",
        f"{stats['winrate']:.1f}%"
    )
    print("--------------------------------")


if __name__ == "__main__":
    main()