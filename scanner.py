#!/usr/bin/env python3
"""Top-400 Hammer Scanner (15m) — CoinLore ranking, OKX candles, Telegram alerts.

Read-only: never places orders. No third-party Python libraries required.

The universe is the top 400 by market cap (CoinLore). For prices the script
uses active OKX USDT perpetual swaps, or OKX USDT spot where no swap exists.
A coin without an OKX USDT market cannot be scanned and is reported as missing.

Strategy:
  SUPPORT   = green candle, body entirely in top 0..0.382 Fibonacci band,
              wick down, >=2 independent, previously confirmed swing lows.
  RESISTANCE= red candle, body entirely in bottom 0..0.382 Fibonacci band,
              wick up, >=2 independent, previously confirmed swing highs.
  Prior support/resistance can be HORIZONTAL or DIAGONAL (rising/falling).
  A diagonal line is anchored to at least 2 distinct confirmed swing pivots,
  must meet the signal wick within tolerance, and must not have been broken
  by two consecutive candle closes. Ties favor horizontal levels.
  A signal is only generated using a CLOSED 15-minute candle.

Telegram configuration: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID as
environment variables (GitHub Actions must explicitly pass repository Secrets).

Examples:
  python scanner.py
  python scanner.py --max-markets 400 --min-touches 2 --output-dir results
  python scanner.py --telegram-test     # send a test, no market API used
  python scanner.py --self-test         # local synthetic tests, no API used
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LOG = logging.getLogger("scanner")
OKX_BASE = "https://www.okx.com"
COINLORE_BASE = "https://api.coinlore.net"
CANDLE_MS = 15 * 60 * 1000
MIN_CANDLES = 40
FIB = 0.382


class ScannerError(RuntimeError):
    """API or configuration error."""


class SharedRateLimiter:
    """Spread OKX requests across concurrent workers."""

    def __init__(self, interval: float = 0.16):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_time = 0.0

    def wait(self) -> None:
        with self.lock:
            now = time.monotonic()
            pause = max(0.0, self.next_time - now)
            self.next_time = max(now, self.next_time) + self.interval
        if pause:
            time.sleep(pause)


OKX_LIMITER = SharedRateLimiter()


def fetch_json(base: str, path: str, params: dict | None = None) -> dict:
    url = base + path
    if params:
        url += "?" + urlencode(params)
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            request = Request(url, headers={
                "Accept": "application/json",
                "User-Agent": "Top400-Hammer-Scanner/2.0",
            })
            with urlopen(request, timeout=18) as response:
                value = json.load(response)
            if not isinstance(value, dict):
                raise ScannerError("Expected JSON object")
            if base == OKX_BASE and str(value.get("code")) != "0":
                raise ScannerError("OKX API returned error code " + str(value.get("code")))
            return value
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, ScannerError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.6 * (2 ** attempt) + random.random() * 0.15)
    raise ScannerError(f"Market data API failed: {base}{path}: {last_error}")


def fetch_top400() -> tuple[dict[str, dict], list[str]]:
    """Fetch exactly the first 400 projects ranked by CoinLore market cap.

    Some unrelated assets share tickers: skip ambiguous tickers rather than
    accidentally attaching a different project's market to them.
    """
    by_symbol: dict[str, dict] = {}
    ambiguous: set[str] = set()
    projects_count = 0
    for start in range(0, 400, 100):
        rows = fetch_json(COINLORE_BASE, "/api/tickers/", {
            "start": start, "limit": 100,
        }).get("data")
        if not isinstance(rows, list) or len(rows) != 100:
            raise ScannerError(f"Top-400 list incomplete at rank {start + 1}")
        for i, item in enumerate(rows):
            rank = int(item.get("rank") or (start + i + 1))
            if not 1 <= rank <= 400:
                continue
            projects_count += 1
            symbol = str(item.get("symbol", "")).strip().upper()
            if not symbol:
                continue
            current = {"rank": rank, "coin": symbol, "project": str(item.get("name", ""))}
            if symbol in by_symbol:
                ambiguous.add(symbol)
            else:
                by_symbol[symbol] = current
    if projects_count < 390:
        raise ScannerError(f"Top-400 list has only {projects_count} entries")
    for symbol in ambiguous:
        by_symbol.pop(symbol, None)
    return by_symbol, sorted(ambiguous)


def okx_usdt_markets(inst_type: str) -> dict[str, dict]:
    """Return eligible OKX USDT instrument by base ticker."""
    rows = fetch_json(OKX_BASE, "/api/v5/public/instruments", {
        "instType": inst_type,
    }).get("data")
    if not isinstance(rows, list):
        raise ScannerError("Invalid OKX instrument response")
    available = {}
    suffix = "-USDT-SWAP" if inst_type == "SWAP" else "-USDT"
    for item in rows:
        inst_id = str(item.get("instId", ""))
        if item.get("state") != "live" or not inst_id.endswith(suffix):
            continue
        if inst_type == "SWAP":
            if item.get("settleCcy") != "USDT" or item.get("ctType") != "linear":
                continue
        elif item.get("quoteCcy") != "USDT":
            continue
        base = inst_id[:-len(suffix)]
        # Only simple USDT pairs, no leveraged indices or exotic quote pairs.
        if base and "-" not in base:
            available[base] = {"okx_inst_id": inst_id, "market_type": inst_type}
    return available


def build_universe(top: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    swaps = okx_usdt_markets("SWAP")
    spots = okx_usdt_markets("SPOT")
    eligible = []
    missing = []
    for symbol, info in sorted(top.items(), key=lambda pair: pair[1]["rank"]):
        market = swaps.get(symbol) or spots.get(symbol)
        if market:
            eligible.append({**info, **market})
        else:
            missing.append({**info, "reason": "no OKX USDT 15m market"})
    return eligible, missing


@dataclass(frozen=True)
class Candle:
    ts: int  # opening timestamp in milliseconds
    o: float
    h: float
    l: float
    c: float


def closed_candles(inst_id: str) -> list[Candle]:
    OKX_LIMITER.wait()
    rows = fetch_json(OKX_BASE, "/api/v5/market/candles", {
        "instId": inst_id, "bar": "15m", "limit": 300,
    }).get("data", [])
    if not isinstance(rows, list):
        raise ScannerError(f"Invalid candle data for {inst_id}")
    bars = {}
    for data in rows:
        if not isinstance(data, list) or len(data) < 9 or str(data[8]) != "1":
            continue  # only confirmed (closed) OKX candles
        try:
            x = Candle(int(data[0]), *(float(v) for v in data[1:5]))
        except (TypeError, ValueError):
            continue
        if x.l <= 0 or x.h <= x.l or not (x.l <= min(x.o, x.c) <= max(x.o, x.c) <= x.h):
            continue
        bars[x.ts] = x
    return sorted(bars.values(), key=lambda x: x.ts)


def historical_pivots(bars: list[Candle], kind: str, radius: int = 2) -> list[int]:
    """Distinct past swing extrema that were confirmed before signal bar."""
    pivots = []
    # Ignore the final candidate candle and nearby, unconfirmed pivot candles.
    for idx in range(radius, len(bars) - radius - 1):
        level = bars[idx].h if kind == "resistance" else bars[idx].l
        around = [bars[j].h if kind == "resistance" else bars[j].l
                  for j in range(idx - radius, idx + radius + 1) if j != idx]
        if (kind == "resistance" and level > max(around)) or (
                kind == "support" and level < min(around)):
            pivots.append(idx)
    return pivots


def prior_level(bars: list[Candle], kind: str, tolerance: float,
                min_touches: int, min_gap: int) -> tuple[float, int] | None:
    target = bars[-1].h if kind == "resistance" else bars[-1].l
    margin = target * tolerance
    candidates = []
    for idx in historical_pivots(bars, kind):
        price = bars[idx].h if kind == "resistance" else bars[idx].l
        distance = abs(price - target)
        if distance <= margin:
            candidates.append((distance, idx, price))
    candidates.sort()
    accepted = []
    for _, idx, price in candidates:
        if all(abs(idx - old_idx) >= min_gap for old_idx, _ in accepted):
            accepted.append((idx, price))
    if len(accepted) < min_touches:
        return None
    return mean(price for _, price in accepted), len(accepted)


def diagonal_level(bars: list[Candle], kind: str, tolerance: float,
                   min_touches: int, min_gap: int) -> tuple[float, int, float] | None:
    """Find an unbroken sloping line fitted through independent confirmed pivots.

    Return (current projected level, number of historical touches, slope/bar).
    Upward and downward sloping support/resistance are both considered.
    """
    pivots = historical_pivots(bars, kind)
    if len(pivots) < min_touches:
        return None
    current_idx = len(bars) - 1
    target = bars[-1].l if kind == "support" else bars[-1].h
    if target <= 0:
        return None
    prices = {i: bars[i].l if kind == "support" else bars[i].h for i in pivots}
    best: tuple[float, int, float] | None = None
    best_score: tuple[int, float, int] | None = None

    for n, first in enumerate(pivots):
        for second in pivots[n + 1:]:
            gap = second - first
            if gap < min_gap:
                continue
            slope = (prices[second] - prices[first]) / gap
            # Separate genuine diagonal levels from virtually flat levels.
            if abs(slope) / target < 0.00003:  # 0.003% per 15-minute bar
                continue
            projected = prices[first] + slope * (current_idx - first)
            if projected <= 0 or abs(projected - target) > target * tolerance:
                continue

            # Identify distinct prior touches of the same projected trendline.
            touches = []
            for idx in pivots:
                if idx < first:
                    continue
                level_here = prices[first] + slope * (idx - first)
                if level_here <= 0:
                    continue
                if abs(prices[idx] - level_here) <= level_here * tolerance:
                    if not touches or idx - touches[-1] >= min_gap:
                        touches.append(idx)
            if len(touches) < min_touches:
                continue

            # Reject lines that were decisively broken after the first anchor.
            # Use closes rather than wicks, and allow a little price noise.
            consecutive_breaks = 0
            broken = False
            for idx in range(first + 1, current_idx):
                level_here = prices[first] + slope * (idx - first)
                if level_here <= 0:
                    broken = True
                    break
                close = bars[idx].c
                beyond = (close < level_here * (1 - 2 * tolerance)
                          if kind == "support"
                          else close > level_here * (1 + 2 * tolerance))
                consecutive_breaks = consecutive_breaks + 1 if beyond else 0
                if consecutive_breaks >= 2:
                    broken = True
                    break
            if broken:
                continue

            score = (len(touches), -abs(projected - target) / target, touches[-1])
            if best_score is None or score > best_score:
                best_score = score
                best = (projected, len(touches), slope)
    return best


def detect_signal(bars: list[Candle], tolerance: float = 0.003,
                  min_touches: int = 2, min_gap: int = 4) -> dict | None:
    """Apply Fibonacci/body/color rules with horizontal OR diagonal SR."""
    if len(bars) < MIN_CANDLES:
        return None
    candle = bars[-1]
    span = candle.h - candle.l
    if span <= 0:
        return None
    body_bottom = min(candle.o, candle.c)
    body_top = max(candle.o, candle.c)
    eps = span * 1e-10

    # Support: wick LOW -> HIGH; body must be in the UPPER 38.2%.
    if candle.c > candle.o and body_bottom >= candle.h - FIB * span - eps:
        kind, signal = "support", "LONG_SETUP"
    # Resistance: reverse direction; body in the LOWER 38.2%.
    elif candle.c < candle.o and body_top <= candle.l + FIB * span + eps:
        kind, signal = "resistance", "SHORT_SETUP"
    else:
        return None

    horizontal = prior_level(bars, kind, tolerance, min_touches, min_gap)
    diagonal = diagonal_level(bars, kind, tolerance, min_touches, min_gap)
    if horizontal is None and diagonal is None:
        return None

    # Prefer the line with more distinct historical tests; on a tie prefer
    # the existing horizontal logic to preserve prior scanner behavior.
    if horizontal is not None and (diagonal is None or horizontal[1] >= diagonal[1]):
        level_price, touches = horizontal
        geometry = "horizontal"
        slope_pct = 0.0
    else:
        assert diagonal is not None
        level_price, touches, slope = diagonal
        geometry = "diagonal"
        slope_pct = 100.0 * slope / level_price

    return {
        "signal": signal, "level_type": kind, "level_geometry": geometry,
        "trend_direction": ("rising" if slope_pct > 0 else "falling" if slope_pct < 0 else "flat"),
        "slope_pct_per_15m": slope_pct,
        "prior_touches": touches, "level_price": level_price,
        "candle_time_utc": datetime.fromtimestamp(candle.ts / 1000, tz=timezone.utc).isoformat(),
        "open": candle.o, "high": candle.h, "low": candle.l, "close": candle.c,
        "fib_zone": "0-0.382", "timeframe": "15m",
    }


def telegram_config() -> tuple[str, str]:
    return (os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            os.getenv("TELEGRAM_CHAT_ID", "").strip())


def send_telegram(message: str) -> None:
    token, chat_id = telegram_config()
    if not token or not chat_id:
        raise ScannerError("Telegram credentials missing (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    # Do not log or print the token or full request URL.
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urlencode({"chat_id": chat_id, "text": message}).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            answer = json.load(response)
        if not answer.get("ok"):
            raise ScannerError("Telegram rejected the message")
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        raise ScannerError("Could not deliver Telegram message") from exc


def scan_one(market: dict, args) -> tuple[dict | None, str]:
    bars = closed_candles(market["okx_inst_id"])
    if len(bars) < MIN_CANDLES:
        return None, "insufficient_candles"
    now_ms = int(time.time() * 1000)
    close_ms = bars[-1].ts + CANDLE_MS
    if close_ms > now_ms + 3000 or now_ms - close_ms > args.max_age_minutes * 60_000:
        return None, "stale_candles"
    hit = detect_signal(bars, args.zone_tolerance, args.min_touches, args.min_gap)
    return ({**market, **hit} if hit else None), "scanned"


def save_csv(filename: Path, records: list[dict], columns: list[str]) -> None:
    filename.parent.mkdir(parents=True, exist_ok=True)
    with filename.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)


def self_test() -> None:
    """Offline regression tests including horizontal and diagonal support/resistance."""
    origin = 1_790_000_000_000
    # Flat history with two separated local low pivots at 20 and 35.
    bars = [Candle(origin + i * CANDLE_MS, 101, 102, 99, 100) for i in range(70)]
    for i in (20, 35):
        bars[i] = Candle(origin + i * CANDLE_MS, 99.6, 101, 96.0, 100)
    # Green hammer; body 101.2..101.7, overall 96..102; top zone [99.708,102]
    bars[-1] = Candle(origin + 69 * CANDLE_MS, 101.2, 102, 96.1, 101.7)
    a = detect_signal(bars)
    assert a and a["signal"] == "LONG_SETUP" and a["prior_touches"] == 2 and a["level_geometry"] == "horizontal", a
    # Reject same hammer when head exceeds 38.2% zone.
    bars[-1] = Candle(origin + 69 * CANDLE_MS, 99.0, 102, 96.1, 101.7)
    assert detect_signal(bars) is None
    # Support needs green candle.
    bars[-1] = Candle(origin + 69 * CANDLE_MS, 101.7, 102, 96.1, 101.2)
    assert detect_signal(bars) is None

    high_bars = [Candle(origin + i * CANDLE_MS, 98, 100, 97, 99) for i in range(70)]
    for i in (20, 35):
        high_bars[i] = Candle(origin + i * CANDLE_MS, 99, 106, 98, 100)
    # Red inverted hammer; body 98.4..98.9; low 98 high 106.1,
    # bottom zone [98,101.0942]
    high_bars[-1] = Candle(origin + 69 * CANDLE_MS, 98.9, 105.9, 98, 98.4)
    b = detect_signal(high_bars)
    assert b and b["signal"] == "SHORT_SETUP" and b["prior_touches"] == 2 and b["level_geometry"] == "horizontal", b
    high_bars[-1] = Candle(origin + 69 * CANDLE_MS, 101.9, 105.9, 98, 98.4)
    assert detect_signal(high_bars) is None
    # Diagonal rising support: two pivot lows follow a rising trendline.
    rising = []
    for i in range(70):
        support_line = 90 + 0.08 * i
        rising.append(Candle(origin + i * CANDLE_MS,
                             support_line + 1.5, support_line + 2.3,
                             support_line + 0.9, support_line + 1.8))
    for i in (15, 40):
        line = 90 + 0.08 * i
        rising[i] = Candle(origin + i * CANDLE_MS, line + 1.5,
                           line + 2.3, line, line + 1.8)
    last_support = 90 + 0.08 * 69
    rising[-1] = Candle(origin + 69 * CANDLE_MS, last_support + 1.8,
                        last_support + 2.5, last_support - 0.06, last_support + 2.1)
    c = detect_signal(rising)
    assert c and c["signal"] == "LONG_SETUP" and c["level_geometry"] == "diagonal" and c["prior_touches"] >= 2 and c["trend_direction"] == "rising", c
    # A decisive close break invalidates the diagonal line.
    broken = rising.copy()
    for i in (55, 56):
        line = 90 + 0.08 * i
        broken[i] = Candle(origin + i * CANDLE_MS, line - 1.3, line - 0.8,
                           line - 1.6, line - 1.2)
    assert detect_signal(broken) is None

    # Diagonal falling resistance: two pivot highs follow a descending line.
    falling = []
    for i in range(70):
        resistance_line = 110 - 0.1 * i
        falling.append(Candle(origin + i * CANDLE_MS,
                              resistance_line - 1.4, resistance_line - 0.8,
                              resistance_line - 3.0, resistance_line - 1.6))
    for i in (15, 40):
        line = 110 - 0.1 * i
        falling[i] = Candle(origin + i * CANDLE_MS, line - 1.4,
                            line, line - 3.0, line - 1.6)
    last_resistance = 110 - 0.1 * 69
    falling[-1] = Candle(origin + 69 * CANDLE_MS, last_resistance - 2.65,
                         last_resistance + 0.06, last_resistance - 3.0, last_resistance - 2.8)
    d = detect_signal(falling)
    assert d and d["signal"] == "SHORT_SETUP" and d["level_geometry"] == "diagonal" and d["prior_touches"] >= 2 and d["trend_direction"] == "falling", d
    LOG.info("Offline self-test passed (horizontal and diagonal support/resistance, Fibonacci, color, pivot touches, invalid breaks)")


def main() -> int:
    parser = argparse.ArgumentParser(description="OKX top-400 15m Fibonacci hammer scanner")
    parser.add_argument("--output-dir", default="results")
    parser.add_argument("--max-markets", type=int, default=400,
                        help="maximum eligible markets; 0 = all available")
    parser.add_argument("--min-touches", type=int, default=2)
    parser.add_argument("--zone-tolerance", type=float, default=0.003,
                        help="distance to pretested support/resistance: 0.003 = 0.3 percent")
    parser.add_argument("--min-gap", type=int, default=4,
                        help="minimum distance between two historical touches in 15m candles")
    parser.add_argument("--max-age-minutes", type=int, default=19,
                        help="max age since signal candle CLOSED, 19 minutes by default")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--list-only", action="store_true")
    parser.add_argument("--telegram-test", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not (args.max_markets >= 0 and args.min_touches >= 2 and args.min_gap >= 3
            and 0 < args.zone_tolerance <= 0.05 and 1 <= args.workers <= 12
            and 1 <= args.max_age_minutes <= 60):
        parser.error("invalid parameter values")

    if args.self_test:
        self_test()
        return 0
    if args.telegram_test:
        try:
            send_telegram("✅ Top-400 Hammer-Scanner: Telegram test successful.")
            LOG.info("Telegram test delivered")
            return 0
        except ScannerError as exc:
            LOG.error("Telegram test failed: %s", exc)
            return 1

    token, chat_id = telegram_config()
    if not (token and chat_id):
        LOG.warning("TELEGRAM IS DISABLED: GitHub Secrets are not passed into this job")
    else:
        LOG.info("Telegram configuration found; signals will be sent if detected")

    output = Path(args.output_dir)
    started = datetime.now(timezone.utc)
    try:
        LOG.info("Loading top 400 projects by market capitalization (no other-exchange filter)")
        top, ambiguous = fetch_top400()
        eligible, missing = build_universe(top)
        if not eligible:
            raise ScannerError("No top-400 coin has a usable OKX market")
        market_fields = ["rank", "coin", "project", "market_type", "okx_inst_id"]
        save_csv(output / "okx_top400_markets.csv", eligible, market_fields)
        save_csv(output / "not_on_okx.csv", missing, ["rank", "coin", "project", "reason"])
        LOG.info("Top-400 ranked: 400; %d OKX markets; %d unavailable; %d ambiguous tickers",
                 len(eligible), len(missing), len(ambiguous))

        markets = eligible[:args.max_markets] if args.max_markets > 0 else eligible
        signals: list[dict] = []
        failed: list[str] = []
        skipped: dict[str, int] = {"insufficient_candles": 0, "stale_candles": 0}
        if not args.list_only:
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                jobs = {pool.submit(scan_one, m, args): m for m in markets}
                for future in as_completed(jobs):
                    m = jobs[future]
                    try:
                        hit, state = future.result()
                        if hit:
                            signals.append(hit)
                        skipped[state] = skipped.get(state, 0) + (0 if state == "scanned" else 1)
                    except Exception as exc:
                        LOG.error("OKX %s unavailable: %s", m["okx_inst_id"], exc)
                        failed.append(m["okx_inst_id"])

            if len(failed) >= max(3, (len(markets) + 4) // 5):
                raise ScannerError(f"Too many OKX API failures ({len(failed)} of {len(markets)})")

        signals.sort(key=lambda s: (s["rank"], s["coin"]))
        signal_fields = market_fields + ["signal", "level_type", "level_geometry", "trend_direction", "slope_pct_per_15m", "prior_touches",
                                         "level_price", "candle_time_utc", "open", "high",
                                         "low", "close", "fib_zone", "timeframe"]
        save_csv(output / "signals.csv", signals, signal_fields)

        delivery_errors = 0
        if not args.list_only:
            for sig in signals:
                direction = "🟢 SUPPORT / LONG SETUP" if sig["signal"] == "LONG_SETUP" else "🔴 RESISTANCE / SHORT SETUP"
                text = (f"{direction}\n{sig['coin']} | OKX {sig['okx_inst_id']} ({sig['market_type']})\n"
                        f"15m candle opened: {sig['candle_time_utc']} UTC\n"
                        f"Level: {sig['level_price']:.10g} | {sig['level_geometry'].upper()} "
                        f"{sig['level_type'].upper()} ({sig['trend_direction']}) | Previous tests: {sig['prior_touches']}\n"
                        f"Fibonacci: 0–0.382 | Candle close: {sig['close']:.10g}\n"
                        "Setup only – decide yourself whether to trade.")
                print(text, flush=True)
                if token and chat_id:
                    try:
                        send_telegram(text)
                    except ScannerError as exc:
                        delivery_errors += 1
                        LOG.error("Telegram delivery error for %s: %s", sig["coin"], exc)
            if signals and not (token and chat_id):
                LOG.error("Found %d signals but did not send Telegram: credentials missing", len(signals))
                delivery_errors = len(signals)

        report = {
            "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            "source": "CoinLore market-cap top 400 and OKX public 15m candles",
            "top_ranked_projects": 400,
            "unique_unambiguous_tickers": len(top),
            "ambiguous_tickers_excluded": ambiguous,
            "okx_markets": len(eligible),
            "top400_missing_okx_markets": len(missing),
            "scanned_markets": 0 if args.list_only else len(markets) - len(failed) - sum(skipped.values()),
            "signal_count": len(signals),
            "horizontal_signals": sum(s["level_geometry"] == "horizontal" for s in signals),
            "diagonal_signals": sum(s["level_geometry"] == "diagonal" for s in signals),
            "market_errors": failed,
            "candle_skips": skipped,
            "telegram_configured": bool(token and chat_id),
            "telegram_delivery_errors": delivery_errors,
            "elapsed_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
        }
        (output / "scan_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        LOG.info("Completed: %d markets, %d signals, %d API errors, %d Telegram errors",
                 report["scanned_markets"], len(signals), len(failed), delivery_errors)
        return 1 if delivery_errors else 0
    except ScannerError as exc:
        LOG.error("Scanner stopped: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
