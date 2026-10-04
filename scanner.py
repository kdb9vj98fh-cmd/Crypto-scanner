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
STRATEGY = "V2.1"

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FIELDS = [
    "time", "strategy", "symbol", "side", "timeframe", "setup_type",
    "entry", "stop", "tp1", "tp2", "tp3", "planned_rr",
    "trigger", "status", "last_checked"
]

TERMINAL = {
    "STOP", "TP3", "UNCLEAR",
    "TP1_THEN_STOP", "TP2_THEN_STOP"
}


def now():
    return datetime.now(timezone.utc).isoformat()


def num(x):
    return format(float(x), ".12g")


def flt(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


# =========================================================
# TELEGRAM
# =========================================================

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
            headers={"User-Agent": "CryptoScanner/V2.1"}
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


# =========================================================
# OKX
# =========================================================

def api(path, params=None):
    if params:
        path += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        BASE + path,
        headers={"User-Agent": "Mozilla/5.0 CryptoScanner/V2.1"}
    )

    with urllib.request.urlopen(req, timeout=20) as r:
        obj = json.loads(r.read().decode())

    if obj.get("code") != "0":
        raise RuntimeError(obj)

    return obj.get("data", [])


def markets():
    instruments = api(
        "/api/v5/public/instruments",
        {"instType": "SWAP"}
    )

    tickers = api(
        "/api/v5/market/tickers",
        {"instType": "SWAP"}
    )

    vols = {
        x.get("instId", ""): flt(x.get("volCcy24h"))
        for x in tickers
    }

    excluded = {
        "USDT", "USDC", "DAI", "FDUSD",
        "TUSD", "USDE", "PYUSD", "USDS", "BUSD"
    }

    out = []

    for x in instruments:
        symbol = x.get("instId", "")
        base = symbol.split("-")[0] if symbol else ""

        if (
            x.get("state") == "live"
            and symbol.endswith("-USDT-SWAP")
            and base not in excluded
        ):
            out.append((symbol, vols.get(symbol, 0)))

    out.sort(key=lambda x: x[1], reverse=True)

    return [x[0] for x in out[:MAX_MARKETS]]


def candles(symbol, bar, limit=120):
    raw = api(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": bar,
            "limit": str(limit)
        }
    )

    out = []

    for x in reversed(raw):

        # Nur abgeschlossene Kerzen
        if len(x) > 8 and x[8] != "1":
            continue

        out.append({
            "ts": int(x[0]),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(x[5])
        })

    return out


# =========================================================
# INDIKATOREN
# =========================================================

def ema(values, period):
    if not values:
        return []

    alpha = 2 / (period + 1)
    out = [values[0]]

    for value in values[1:]:
        out.append(
            alpha * value
            + (1 - alpha) * out[-1]
        )

    return out


def rsi(values, period=14):
    if len(values) < period + 2:
        return None

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        gains.append(max(change, 0.0))
        losses.append(max(-change, 0.0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (
            avg_gain * (period - 1)
            + gains[i]
        ) / period

        avg_loss = (
            avg_loss * (period - 1)
            + losses[i]
        ) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - 100 / (1 + rs)


def atr(data, period=14):
    if len(data) < 2:
        return 0

    tr = []

    for i in range(1, len(data)):
        tr.append(
            max(
                data[i]["high"] - data[i]["low"],
                abs(
                    data[i]["high"]
                    - data[i - 1]["close"]
                ),
                abs(
                    data[i]["low"]
                    - data[i - 1]["close"]
                )
            )
        )

    values = tr[-period:]

    return (
        sum(values) / len(values)
        if values
        else 0
    )


def avgvol(data, period=20):
    values = [
        x["volume"]
        for x in data[-period:]
    ]

    return (
        sum(values) / len(values)
        if values
        else 0
    )


# =========================================================
# CANDLE PATTERNS
# =========================================================

def body(c):
    return abs(
        c["close"] - c["open"]
    )


def candle_range(c):
    return max(
        c["high"] - c["low"],
        1e-12
    )


def lower_wick(c):
    return (
        min(c["open"], c["close"])
        - c["low"]
    )


def upper_wick(c):
    return (
        c["high"]
        - max(c["open"], c["close"])
    )


def bull(c):
    return c["close"] > c["open"]


def bear(c):
    return c["close"] < c["open"]


def bull_engulf(previous, current):
    return (
        bear(previous)
        and bull(current)
        and current["open"] <= previous["close"]
        and current["close"] >= previous["open"]
    )


def bear_engulf(previous, current):
    return (
        bull(previous)
        and bear(current)
        and current["open"] >= previous["close"]
        and current["close"] <= previous["open"]
    )


def bull_reject(c):
    return (
        lower_wick(c)
        >= max(
            body(c) * 1.5,
            candle_range(c) * 0.30
        )
        and c["close"]
        >= c["low"] + candle_range(c) * 0.60
    )


def bear_reject(c):
    return (
        upper_wick(c)
        >= max(
            body(c) * 1.5,
            candle_range(c) * 0.30
        )
        and c["close"]
        <= c["low"] + candle_range(c) * 0.40
    )


def bull_outside(previous, current):
    return (
        current["high"] > previous["high"]
        and current["low"] < previous["low"]
        and bull(current)
    )


def bear_outside(previous, current):
    return (
        current["high"] > previous["high"]
        and current["low"] < previous["low"]
        and bear(current)
    )


def displacement(data, side):
    if len(data) < 10:
        return False

    average_body = (
        sum(
            body(x)
            for x in data[-10:-1]
        ) / 9
    )

    if average_body <= 0:
        return False

    current = data[-1]

    if side == "LONG":
        return (
            bull(current)
            and body(current)
            >= average_body * 1.5
        )

    return (
        bear(current)
        and body(current)
        >= average_body * 1.5
    )


# =========================================================
# 1H TREND
# =========================================================

def trend_1h(data):
    if len(data) < 60:
        return "NEUTRAL"

    closes = [
        x["close"]
        for x in data
    ]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)

    # V2.1:
    # Trend bleibt Pflicht.
    # Der zusätzliche harte HH/HL bzw.
    # LH/LL-Filter aus V2 wurde entfernt.

    if (
        closes[-1] > e20[-1] > e50[-1]
        and e20[-1] > e20[-5]
    ):
        return "LONG"

    if (
        closes[-1] < e20[-1] < e50[-1]
        and e20[-1] < e20[-5]
    ):
        return "SHORT"

    return "NEUTRAL"


# =========================================================
# 30M SETUP
# =========================================================

def setup_30m(data, side):
    if len(data) < 30:
        return None

    current = data[-1]
    previous = data[-2]

    history = data[-22:-2]

    resistance = max(
        x["high"]
        for x in history
    )

    support = min(
        x["low"]
        for x in history
    )

    a = atr(data)

    if a <= 0:
        return None

    tolerance = a * 0.35

    average_volume = avgvol(data[:-1])

    volume_ratio = (
        current["volume"] / average_volume
        if average_volume
        else 0
    )

    # LONG
    if side == "LONG":

        retest = (
            previous["close"] > resistance
            and resistance - tolerance
            <= current["low"]
            <= resistance + tolerance
            and current["close"] > resistance
            and bull(current)
            and (
                bull_reject(current)
                or bull_engulf(
                    previous,
                    current
                )
            )
        )

        breakout = (
            current["close"] > resistance
            and bull(current)
            and volume_ratio >= 0.75
        )

        if retest:
            return "30m Breakout + Retest"

        if breakout:
            return "30m bestätigter Breakout"

    # SHORT
    if side == "SHORT":

        retest = (
            previous["close"] < support
            and support - tolerance
            <= current["high"]
            <= support + tolerance
            and current["close"] < support
            and bear(current)
            and (
                bear_reject(current)
                or bear_engulf(
                    previous,
                    current
                )
            )
        )

        breakdown = (
            current["close"] < support
            and bear(current)
            and volume_ratio >= 0.85
        )

        if retest:
            return "30m Breakdown + Retest"

        if breakdown:
            return "30m bestätigter Breakdown"

    return None


# =========================================================
# 15M TRIGGER
# =========================================================

def trigger_15m(data, side):
    if len(data) < 30:
        return None

    current = data[-1]
    previous = data[-2]

    recent = data[-8:-1]

    r = rsi(
        [x["close"] for x in data],
        14
    )

    if r is None:
        return None

    average_volume = avgvol(
        data[:-1]
    )

    volume_ratio = (
        current["volume"] / average_volume
        if average_volume
        else 0
    )

    if side == "LONG":

        pattern = (
            bull_engulf(previous, current)
            or bull_reject(current)
            or bull_outside(previous, current)
            or displacement(data, "LONG")
        )

        structure_break = (
            current["close"]
            > max(
                x["high"]
                for x in recent
            )
        )

        if (
            pattern
            and structure_break
            and 45 <= r <= 72
            and volume_ratio >= 0.65
        ):
            return (
                "Bullische 15m-Bestätigung"
                " + Strukturbruch"
                f" + RSI {r:.1f}"
                f" + Volumen {volume_ratio:.2f}x"
            )

    else:

        pattern = (
            bear_engulf(previous, current)
            or bear_reject(current)
            or bear_outside(previous, current)
            or displacement(data, "SHORT")
        )

        structure_break = (
            current["close"]
            < min(
                x["low"]
                for x in recent
            )
        )

        if (
            pattern
            and structure_break
            and 28 <= r <= 55
            and volume_ratio >= 0.75
        ):
            return (
                "Bärische 15m-Bestätigung"
                " + Strukturbruch"
                f" + RSI {r:.1f}"
                f" + Volumen {volume_ratio:.2f}x"
            )

    return None


# =========================================================
# TRADE
# =========================================================

def trade(
    symbol,
    side,
    setup,
    trigger,
    data
):

    entry = data[-1]["close"]

    a = atr(data)

    if a <= 0:
        return None

    recent = data[-8:]

    if side == "LONG":

        stop = min(
            min(
                x["low"]
                for x in recent
            ) - a * 0.10,
            entry - a * 0.80
        )

        risk = entry - stop

        if risk <= 0:
            return None

        tp1 = entry + risk * 1.25
        tp2 = entry + risk * 2
        tp3 = entry + risk * 3

    else:

        stop = max(
            max(
                x["high"]
                for x in recent
            ) + a * 0.10,
            entry + a * 0.80
        )

        risk = stop - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * 1.25
        tp2 = entry - risk * 2
        tp3 = entry - risk * 3

    timestamp = now()

    return {
        "time": timestamp,
        "strategy": STRATEGY,
        "symbol": symbol,
        "side": side,
        "timeframe": "1h/30m/15m",
        "setup_type": setup,
        "entry": num(entry),
        "stop": num(stop),
        "tp1": num(tp1),
        "tp2": num(tp2),
        "tp3": num(tp3),
        "planned_rr": "3R TP3",
        "trigger": trigger,
        "status": "OPEN",
        "last_checked": timestamp
    }


# =========================================================
# JOURNAL
# =========================================================

def load():
    if not os.path.exists(JOURNAL):
        return []

    with open(
        JOURNAL,
        newline="",
        encoding="utf-8"
    ) as f:
        rows = list(
            csv.DictReader(f)
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
        encoding="utf-8"
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=FIELDS
        )

        writer.writeheader()

        for row in rows:
            writer.writerow({
                key: row.get(key, "")
                for key in FIELDS
            })


def strategy_rows(rows):
    return [
        row
        for row in rows
        if row.get("strategy") == STRATEGY
    ]


def duplicate(
    rows,
    symbol,
    side
):

    return any(
        row.get("strategy") == STRATEGY
        and row.get("symbol") == symbol
        and row.get("side") == side
        and row.get("status")
        in {"OPEN", "TP1", "TP2"}
        for row in rows
    )


# =========================================================
# STATISTIK
# =========================================================

def test_stats(rows):

    test = strategy_rows(rows)

    completed = [
        row
        for row in test
        if row.get("status")
        in TERMINAL
    ]

    tp3 = [
        row
        for row in completed
        if row.get("status") == "TP3"
    ]

    rate = (
        len(tp3)
        / len(completed)
        * 100
        if completed
        else 0
    )

    return {
        "total": len(test),

        "completed":
            len(completed),

        "open":
            len(test)
            - len(completed),

        "tp3":
            len(tp3),

        "stop":
            sum(
                row.get("status")
                == "STOP"
                for row in completed
            ),

        "partial":
            sum(
                row.get("status")
                in {
                    "TP1_THEN_STOP",
                    "TP2_THEN_STOP"
                }
                for row in completed
            ),

        "unclear":
            sum(
                row.get("status")
                == "UNCLEAR"
                for row in completed
            ),

        "rate":
            rate
    }


# =========================================================
# NEUES V2.1 SIGNAL
# =========================================================

def notify_trade(
    trade_data,
    trade_number
):

    direction = (
        "🟢 LONG"
        if trade_data["side"] == "LONG"
        else "🔴 SHORT"
    )

    telegram(
        f"🚨 NEUES V2.1-SIGNAL "
        f"— {trade_number}/{TEST_LIMIT}\n\n"

        f"Paar: {trade_data['symbol']}\n"
        f"Richtung: {direction}\n"
        f"Zeitrahmen: "
        f"{trade_data['timeframe']}\n"

        f"Setup: "
        f"{trade_data['setup_type']}\n"

        f"Bestätigung: "
        f"{trade_data['trigger']}\n\n"

        f"Einstieg: "
        f"{trade_data['entry']}\n"

        f"Stop-Loss: "
        f"{trade_data['stop']}\n"

        f"Ziel 1: "
        f"{trade_data['tp1']}\n"

        f"Ziel 2: "
        f"{trade_data['tp2']}\n"

        f"Ziel 3: "
        f"{trade_data['tp3']}\n"

        f"TP3-Ziel: 3R"
    )


# =========================================================
# STATUS-UPDATE
# NUR V2.1 !!!
# =========================================================

def update_journal(rows):

    changes = []

    for row in rows:

        # ---------------------------------------------
        # ENTSCHEIDENDE ÄNDERUNG:
        #
        # CURRENT
        # CREAMER_CRYPTO
        # V2
        #
        # werden komplett ignoriert.
        #
        # Nur V2.1 darf Telegram-Nachrichten erzeugen.
        # ---------------------------------------------

        if row.get("strategy") != STRATEGY:
            continue

        if (
            row.get("status") in TERMINAL
            or row.get("status")
            not in {"OPEN", "TP1", "TP2"}
        ):
            continue

        try:

            start = int(
                datetime.fromisoformat(
                    row["time"].replace(
                        "Z",
                        "+00:00"
                    )
                ).timestamp() * 1000
            )

            data = [
                candle
                for candle in candles(
                    row["symbol"],
                    "15m",
                    100
                )
                if candle["ts"] > start
            ]

            old_status = row["status"]

            progress = {
                "OPEN": 0,
                "TP1": 1,
                "TP2": 2
            }.get(
                old_status,
                0
            )

            stop = flt(row["stop"])
            tp1 = flt(row["tp1"])
            tp2 = flt(row["tp2"])
            tp3 = flt(row["tp3"])

            status = old_status

            for candle in data:

                if row["side"] == "LONG":

                    stop_hit = (
                        candle["low"]
                        <= stop
                    )

                    hits = [
                        candle["high"] >= tp1,
                        candle["high"] >= tp2,
                        candle["high"] >= tp3
                    ]

                else:

                    stop_hit = (
                        candle["high"]
                        >= stop
                    )

                    hits = [
                        candle["low"] <= tp1,
                        candle["low"] <= tp2,
                        candle["low"] <= tp3
                    ]

                highest = (
                    3 if hits[2]
                    else 2 if hits[1]
                    else 1 if hits[0]
                    else 0
                )

                # Stop und neues Ziel
                # innerhalb derselben Kerze.
                if (
                    stop_hit
                    and highest > progress
                ):
                    status = "UNCLEAR"
                    break

                if highest > progress:

                    progress = highest

                    status = {
                        1: "TP1",
                        2: "TP2",
                        3: "TP3"
                    }[progress]

                    if progress == 3:
                        break

                if stop_hit:

                    if progress == 0:
                        status = "STOP"
                    else:
                        status = (
                            f"TP{progress}"
                            "_THEN_STOP"
                        )

                    break

            row["status"] = status
            row["last_checked"] = now()

            # Nur V2.1 kommt bis hier.
            if status != old_status:

                message = (
                    "📌 V2.1 SIGNAL-UPDATE\n\n"
                    f"Paar: {row['symbol']}\n"
                    f"Richtung: {row['side']}\n"
                    f"Status: "
                    f"{old_status} → {status}"
                )

                changes.append(message)

                telegram(message)

        except Exception as e:
            print(
                "Journal error",
                row.get("symbol"),
                e
            )

        time.sleep(PAUSE)

    return changes


# =========================================================
# SCAN
# =========================================================

def scan(symbol, rows):

    h1 = candles(
        symbol,
        "1H",
        100
    )

    time.sleep(PAUSE)

    m30 = candles(
        symbol,
        "30m",
        120
    )

    time.sleep(PAUSE)

    m15 = candles(
        symbol,
        "15m",
        120
    )

    side = trend_1h(h1)

    if side not in {
        "LONG",
        "SHORT"
    }:
        return None

    setup = setup_30m(
        m30,
        side
    )

    if not setup:
        return None

    trigger = trigger_15m(
        m15,
        side
    )

    if not trigger:
        return None

    if duplicate(
        rows,
        symbol,
        side
    ):
        return None

    return trade(
        symbol,
        side,
        setup,
        trigger,
        m15
    )


# =========================================================
# MAIN
# =========================================================

def main():

    print(
        "Crypto Scanner V2.1 gestartet"
    )

    rows = load()

    # Ausschließlich V2.1 wird
    # weiter überwacht.
    changes = update_journal(rows)

    try:
        market_list = markets()

    except Exception as e:

        telegram(
            "❌ V2.1 Scanner-Fehler: "
            "Market-Liste nicht geladen\n"
            f"{e}"
        )

        save(rows)
        raise

    scanned = 0
    errors = 0
    new_signals = 0

    # 100 neue V2.1-Signale maximal.
    if (
        len(strategy_rows(rows))
        >= TEST_LIMIT
    ):

        print(
            "V2.1-Testlimit erreicht: "
            "keine neuen Signale."
        )

    else:

        for i, symbol in enumerate(
            market_list,
            1
        ):

            if (
                len(strategy_rows(rows))
                >= TEST_LIMIT
            ):
                break

            try:

                found = scan(
                    symbol,
                    rows
                )

                scanned += 1

                if found:

                    rows.append(found)

                    new_signals += 1

                    trade_number = len(
                        strategy_rows(rows)
                    )

                    print(
                        f"NEW [V2.1]: "
                        f"{symbol} "
                        f"{found['side']} "
                        f"#{trade_number}"
                    )

                    notify_trade(
                        found,
                        trade_number
                    )

            except Exception as e:

                errors += 1

                print(
                    "ERROR",
                    symbol,
                    e
                )

            if i % 10 == 0:

                print(
                    f"Progress: "
                    f"{i}/"
                    f"{len(market_list)}"
                )

            time.sleep(PAUSE)

    save(rows)

    stats = test_stats(rows)

    print(
        "Successfully scanned:",
        scanned
    )

    print(
        "Errors:",
        errors
    )

    print(
        "Neue V2.1-Signale:",
        new_signals
    )

    print(
        "V2.1 total:",
        stats["total"]
    )

    print(
        "V2.1 completed:",
        stats["completed"]
    )

    print(
        "V2.1 TP3:",
        stats["tp3"]
    )

    print(
        "V2.1 TP3 rate:",
        f'{stats["rate"]:.1f}%'
    )

    # Nur bei manuellem GitHub-Start
    # kommt zusätzlich eine Statusmeldung.
    if (
        os.environ.get(
            "GITHUB_EVENT_NAME"
        )
        == "workflow_dispatch"
    ):

        telegram(
            "✅ CRYPTO-SCANNER V2.1 AKTIV\n\n"

            f"Märkte geprüft: "
            f"{scanned}/"
            f"{len(market_list)}\n"

            f"Fehler: {errors}\n"

            f"Neue V2.1-Signale: "
            f"{new_signals}\n"

            f"V2.1-Updates: "
            f"{len(changes)}\n\n"

            f"V2.1-Test: "
            f"{stats['total']}/"
            f"{TEST_LIMIT} Signale\n"

            f"Abgeschlossen: "
            f"{stats['completed']}\n"

            f"Noch offen: "
            f"{stats['open']}\n"

            f"TP3 komplett: "
            f"{stats['tp3']}\n"

            f"Direkt Stop: "
            f"{stats['stop']}\n"

            f"Teilziel → Stop: "
            f"{stats['partial']}\n"

            f"Unklar: "
            f"{stats['unclear']}\n"

            f"TP3-Quote: "
            f"{stats['rate']:.1f}%"
        )


if __name__ == "__main__":
    main()