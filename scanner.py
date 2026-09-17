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

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

FIELDS = [
    "time", "symbol", "side", "timeframe", "setup_type",
    "entry", "stop", "tp1", "tp2", "tp3",
    "planned_rr", "trigger", "status", "last_checked",
]


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram: Secrets fehlen")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"

    data = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "disable_web_page_preview": "true",
    }).encode()

    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "CryptoScanner/1.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.loads(response.read().decode())

        if not result.get("ok"):
            raise RuntimeError(result)

        print("Telegram: Nachricht gesendet")
        return True

    except Exception as e:
        print(f"TELEGRAM ERROR: {e}")
        return False


# ============================================================
# OKX
# ============================================================

def api_get(path, params=None):
    if params:
        path += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(
        BASE + path,
        headers={"User-Agent": "Mozilla/5.0"},
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        obj = json.loads(response.read().decode())

    if obj.get("code") != "0":
        raise RuntimeError(obj)

    return obj["data"]


def get_markets():
    instruments = api_get(
        "/api/v5/public/instruments",
        {"instType": "SWAP"},
    )

    tickers = api_get(
        "/api/v5/market/tickers",
        {"instType": "SWAP"},
    )

    volumes = {}

    for ticker in tickers:
        try:
            volumes[ticker["instId"]] = float(
                ticker.get("volCcy24h") or 0
            )
        except Exception:
            volumes[ticker["instId"]] = 0

    markets = []

    for inst in instruments:
        inst_id = inst.get("instId", "")

        if (
            inst.get("state") == "live"
            and inst_id.endswith("-USDT-SWAP")
        ):
            markets.append(
                (inst_id, volumes.get(inst_id, 0))
            )

    markets.sort(key=lambda x: x[1], reverse=True)

    return [x[0] for x in markets[:MAX_MARKETS]]


def candles(symbol, bar, limit=120):
    data = api_get(
        "/api/v5/market/candles",
        {
            "instId": symbol,
            "bar": bar,
            "limit": str(limit),
        },
    )

    result = []

    for x in reversed(data):
        # Nur geschlossene Kerzen
        if len(x) > 8 and x[8] != "1":
            continue

        result.append({
            "ts": int(x[0]),
            "open": float(x[1]),
            "high": float(x[2]),
            "low": float(x[3]),
            "close": float(x[4]),
            "volume": float(x[5]),
        })

    return result


# ============================================================
# INDICATORS
# ============================================================

def ema(values, period):
    if not values:
        return []

    alpha = 2 / (period + 1)
    result = [values[0]]

    for value in values[1:]:
        result.append(
            alpha * value + (1 - alpha) * result[-1]
        )

    return result


def atr(data, period=14):
    if len(data) < 2:
        return 0

    tr = []

    for i in range(1, len(data)):
        high = data[i]["high"]
        low = data[i]["low"]
        prev_close = data[i - 1]["close"]

        tr.append(
            max(
                high - low,
                abs(high - prev_close),
                abs(low - prev_close),
            )
        )

    values = tr[-period:]

    if not values:
        return 0

    return sum(values) / len(values)


def average_volume(data, period=20):
    if not data:
        return 0

    values = [x["volume"] for x in data[-period:]]

    return sum(values) / len(values)


# ============================================================
# CANDLE HELPERS
# ============================================================

def body(c):
    return abs(c["close"] - c["open"])


def candle_range(c):
    return max(c["high"] - c["low"], 1e-12)


def bullish(c):
    return c["close"] > c["open"]


def bearish(c):
    return c["close"] < c["open"]


def bullish_engulfing(prev, cur):
    return (
        bearish(prev)
        and bullish(cur)
        and cur["open"] <= prev["close"]
        and cur["close"] >= prev["open"]
    )


def bearish_engulfing(prev, cur):
    return (
        bullish(prev)
        and bearish(cur)
        and cur["open"] >= prev["close"]
        and cur["close"] <= prev["open"]
    )


def bullish_rejection(c):
    rng = candle_range(c)

    lower_wick = (
        min(c["open"], c["close"]) - c["low"]
    )

    return (
        lower_wick >= body(c) * 1.5
        and lower_wick / rng >= 0.35
        and c["close"] > c["low"] + rng * 0.55
    )


def bearish_rejection(c):
    rng = candle_range(c)

    upper_wick = (
        c["high"] - max(c["open"], c["close"])
    )

    return (
        upper_wick >= body(c) * 1.5
        and upper_wick / rng >= 0.35
        and c["close"] < c["low"] + rng * 0.45
    )


def bullish_outside(prev, cur):
    return (
        cur["high"] > prev["high"]
        and cur["low"] < prev["low"]
        and bullish(cur)
    )


def bearish_outside(prev, cur):
    return (
        cur["high"] > prev["high"]
        and cur["low"] < prev["low"]
        and bearish(cur)
    )


def bullish_displacement(data):
    if len(data) < 10:
        return False

    cur = data[-1]

    avg_body = sum(
        body(x) for x in data[-10:-1]
    ) / 9

    return (
        bullish(cur)
        and body(cur) >= avg_body * 1.5
    )


def bearish_displacement(data):
    if len(data) < 10:
        return False

    cur = data[-1]

    avg_body = sum(
        body(x) for x in data[-10:-1]
    ) / 9

    return (
        bearish(cur)
        and body(cur) >= avg_body * 1.5
    )


# ============================================================
# 1H BIAS
# ============================================================

def get_bias(data):
    if len(data) < 50:
        return "NEUTRAL"

    closes = [x["close"] for x in data]

    e20 = ema(closes, 20)[-1]
    e50 = ema(closes, 50)[-1]
    price = closes[-1]

    if price > e20 > e50:
        return "LONG"

    if price < e20 < e50:
        return "SHORT"

    return "NEUTRAL"


# ============================================================
# 30M SETUP
# ============================================================

def setup_30m(data):
    if len(data) < 25:
        return None

    cur = data[-1]
    prev = data[-2]

    history = data[-22:-2]

    resistance = max(x["high"] for x in history)
    support = min(x["low"] for x in history)

    avg_vol = average_volume(data[:-1], 20)

    volume_ok = (
        avg_vol > 0
        and cur["volume"] >= avg_vol * 0.8
    )

    # Breakout
    if cur["close"] > resistance and bullish(cur):
        return {
            "side": "LONG",
            "type": "Breakout",
            "level": resistance,
            "volume_ok": volume_ok,
        }

    # Breakdown
    if cur["close"] < support and bearish(cur):
        return {
            "side": "SHORT",
            "type": "Breakdown",
            "level": support,
            "volume_ok": volume_ok,
        }

    # Sweep unten + Reclaim
    if (
        cur["low"] < support
        and cur["close"] > support
        and bullish(cur)
    ):
        return {
            "side": "LONG",
            "type": "Liquidity Sweep/Reclaim",
            "level": support,
            "volume_ok": volume_ok,
        }

    # Sweep oben + Reclaim
    if (
        cur["high"] > resistance
        and cur["close"] < resistance
        and bearish(cur)
    ):
        return {
            "side": "SHORT",
            "type": "Liquidity Sweep/Reclaim",
            "level": resistance,
            "volume_ok": volume_ok,
        }

    tolerance = atr(data, 14) * 0.35

    # Support Rejection
    if (
        abs(cur["low"] - support) <= tolerance
        and (
            bullish_rejection(cur)
            or bullish_engulfing(prev, cur)
        )
    ):
        return {
            "side": "LONG",
            "type": "Support Rejection",
            "level": support,
            "volume_ok": volume_ok,
        }

    # Resistance Rejection
    if (
        abs(cur["high"] - resistance) <= tolerance
        and (
            bearish_rejection(cur)
            or bearish_engulfing(prev, cur)
        )
    ):
        return {
            "side": "SHORT",
            "type": "Resistance Rejection",
            "level": resistance,
            "volume_ok": volume_ok,
        }

    return None


# ============================================================
# 15M TRIGGER
# ============================================================

def trigger_15m(data, side):
    if len(data) < 25:
        return None

    cur = data[-1]
    prev = data[-2]

    avg_vol = average_volume(data[:-1], 20)

    volume_ok = (
        avg_vol > 0
        and cur["volume"] >= avg_vol * 0.8
    )

    recent = data[-8:-1]

    if side == "LONG":
        pattern = (
            bullish_engulfing(prev, cur)
            or bullish_rejection(cur)
            or bullish_outside(prev, cur)
            or bullish_displacement(data)
        )

        structure_break = (
            cur["close"]
            > max(x["high"] for x in recent)
        )

        if pattern and (structure_break or volume_ok):
            return (
                "Bullish candle confirmation "
                "+ 15m structure/momentum"
            )

    if side == "SHORT":
        pattern = (
            bearish_engulfing(prev, cur)
            or bearish_rejection(cur)
            or bearish_outside(prev, cur)
            or bearish_displacement(data)
        )

        structure_break = (
            cur["close"]
            < min(x["low"] for x in recent)
        )

        if pattern and (structure_break or volume_ok):
            return (
                "Bearish candle confirmation "
                "+ 15m structure/momentum"
            )

    return None


# ============================================================
# TRADE
# ============================================================

def make_trade(symbol, side, setup, trigger, data15):
    entry = data15[-1]["close"]
    current_atr = atr(data15, 14)

    recent = data15[-6:]

    if side == "LONG":
        structural = min(x["low"] for x in recent)

        stop = min(
            structural,
            entry - current_atr * 0.8,
        )

        risk = entry - stop

        if risk <= 0:
            return None

        tp1 = entry + risk * 1.25
        tp2 = entry + risk * 2
        tp3 = entry + risk * 3

    else:
        structural = max(x["high"] for x in recent)

        stop = max(
            structural,
            entry + current_atr * 0.8,
        )

        risk = stop - entry

        if risk <= 0:
            return None

        tp1 = entry - risk * 1.25
        tp2 = entry - risk * 2
        tp3 = entry - risk * 3

    now = datetime.now(timezone.utc).isoformat()

    return {
        "time": now,
        "symbol": symbol,
        "side": side,
        "timeframe": "30m/15m",
        "setup_type": setup["type"],
        "entry": format(entry, ".12g"),
        "stop": format(stop, ".12g"),
        "tp1": format(tp1, ".12g"),
        "tp2": format(tp2, ".12g"),
        "tp3": format(tp3, ".12g"),
        "planned_rr": "2",
        "trigger": trigger,
        "status": "OPEN",
        "last_checked": now,
    }


# ============================================================
# JOURNAL
# ============================================================

def load_journal():
    if not os.path.exists(JOURNAL):
        return []

    with open(
        JOURNAL,
        "r",
        newline="",
        encoding="utf-8",
    ) as f:
        return list(csv.DictReader(f))


def save_journal(rows):
    with open(
        JOURNAL,
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.DictWriter(
            f,
            fieldnames=FIELDS,
        )

        writer.writeheader()

        for row in rows:
            writer.writerow({
                key: row.get(key, "")
                for key in FIELDS
            })


def duplicate(rows, symbol, side):
    for row in rows:
        if (
            row.get("symbol") == symbol
            and row.get("side") == side
            and row.get("status")
            in {"OPEN", "TP1", "TP2"}
        ):
            return True

    return False


# ============================================================
# OUTCOME CHECK
# ============================================================

def update_outcomes(rows):
    changes = []

    for row in rows:
        old_status = row.get("status", "OPEN")

        if old_status not in {"OPEN", "TP1", "TP2"}:
            continue

        try:
            data = candles(
                row["symbol"],
                "15m",
                100,
            )
        except Exception:
            continue

        try:
            stop = float(row["stop"])
            tp1 = float(row["tp1"])
            tp2 = float(row["tp2"])
            tp3 = float(row["tp3"])
        except Exception:
            continue

        side = row["side"]

        try:
            signal_time = datetime.fromisoformat(
                row["time"].replace("Z", "+00:00")
            ).timestamp() * 1000
        except Exception:
            signal_time = 0

        relevant = [
            c for c in data
            if c["ts"] >= signal_time
        ]

        new_status = old_status

        for c in relevant:
            if side == "LONG":
                stop_hit = c["low"] <= stop
                tp1_hit = c["high"] >= tp1
                tp2_hit = c["high"] >= tp2
                tp3_hit = c["high"] >= tp3

            else:
                stop_hit = c["high"] >= stop
                tp1_hit = c["low"] <= tp1
                tp2_hit = c["low"] <= tp2
                tp3_hit = c["low"] <= tp3

            target_hit = (
                tp1_hit
                or tp2_hit
                or tp3_hit
            )

            if stop_hit and target_hit:
                new_status = "UNCLEAR"
                break

            if stop_hit:
                new_status = "STOP"
                break

            if tp3_hit:
                new_status = "TP3"
                break

            if tp2_hit:
                new_status = "TP2"

            elif tp1_hit:
                if new_status == "OPEN":
                    new_status = "TP1"

        row["last_checked"] = (
            datetime.now(timezone.utc).isoformat()
        )

        if new_status != old_status:
            row["status"] = new_status

            changes.append(
                (
                    row["symbol"],
                    row["side"],
                    old_status,
                    new_status,
                )
            )

    return changes


# ============================================================
# TELEGRAM ALERTS
# ============================================================

def send_trade_alert(trade):
    message = (
        "🚨 NEUES SETUP\n\n"
        f"{trade['symbol']} {trade['side']}\n\n"
        f"Setup: {trade['setup_type']}\n"
        f"Timeframe: {trade['timeframe']}\n"
        f"Trigger: {trade['trigger']}\n\n"
        f"Entry: {trade['entry']}\n"
        f"Stop: {trade['stop']}\n\n"
        f"TP1: {trade['tp1']}\n"
        f"TP2: {trade['tp2']}\n"
        f"TP3: {trade['tp3']}\n\n"
        f"CRV zu TP2: 1:{trade['planned_rr']}"
    )

    send_telegram(message)


def send_journal_update(change):
    symbol, side, old, new = change

    if new == "STOP":
        icon = "🛑"
    elif new == "UNCLEAR":
        icon = "⚠️"
    else:
        icon = "🎯"

    message = (
        f"{icon} JOURNAL UPDATE\n\n"
        f"{symbol} {side}\n"
        f"{old} → {new}"
    )

    send_telegram(message)


# ============================================================
# MAIN
# ============================================================

def main():
    print("Crypto Scanner gestartet")

    rows = load_journal()

    # Alte Trades prüfen
    changes = update_outcomes(rows)

    for change in changes:
        send_journal_update(change)

    try:
        markets = get_markets()

    except Exception as e:
        print(f"MARKET ERROR: {e}")
        save_journal(rows)
        return

    print(f"OKX markets selected: {len(markets)}")

    scanned = 0
    errors = 0
    signals = []

    for index, symbol in enumerate(
        markets,
        start=1,
    ):
        try:
            data1h = candles(
                symbol,
                "1H",
                100,
            )

            data30 = candles(
                symbol,
                "30m",
                100,
            )

            data15 = candles(
                symbol,
                "15m",
                100,
            )

            if (
                len(data1h) < 50
                or len(data30) < 25
                or len(data15) < 25
            ):
                continue

            scanned += 1

            bias = get_bias(data1h)
            setup = setup_30m(data30)

            if not setup:
                continue

            side = setup["side"]

            # Gegen klaren 1H-Trend keine Entries
            if bias == "LONG" and side == "SHORT":
                continue

            if bias == "SHORT" and side == "LONG":
                continue

            trigger = trigger_15m(
                data15,
                side,
            )

            if not trigger:
                continue

            if duplicate(
                rows,
                symbol,
                side,
            ):
                continue

            trade = make_trade(
                symbol,
                side,
                setup,
                trigger,
                data15,
            )

            if not trade:
                continue

            rows.append(trade)
            signals.append(trade)

            print(
                f"NEW: {symbol} "
                f"{side} "
                f"{setup['type']}"
            )

            # Neues bestätigtes Setup sofort senden
            send_trade_alert(trade)

        except Exception as e:
            errors += 1
            print(f"ERROR {symbol}: {e}")

        if index % 10 == 0:
            print(
                f"Progress: "
                f"{index}/{len(markets)}"
            )

        time.sleep(0.05)

    save_journal(rows)

    print(f"Successfully scanned: {scanned}")
    print(f"Errors: {errors}")
    print(f"Confirmed setups: {len(signals)}")
    print(f"Journal updates: {len(changes)}")

    # Bei manuellem GitHub-Start immer Testmeldung schicken.
    # Automatische Läufe schicken nur neue Setups/Updates.
    if (
        os.environ.get("GITHUB_EVENT_NAME")
        == "workflow_dispatch"
    ):
        send_telegram(
            "✅ CRYPTO SCANNER AKTIV\n\n"
            f"Märkte geprüft: {scanned}\n"
            f"Fehler: {errors}\n"
            f"Neue Setups: {len(signals)}\n"
            f"Journal Updates: {len(changes)}"
        )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
