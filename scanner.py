#!/usr/bin/env python3
"""Read-only 15m reversal scanner: OKX candles, BloFin live USDT swaps, top 400 by market cap.
No API keys, no orders. Python 3.10+ standard library only.
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
OKX = "https://www.okx.com"
BLOFIN = "https://openapi.blofin.com"
COINLORE = "https://api.coinlore.net"
MIN_CANDLES = 40
BAR_MS = 15 * 60 * 1000


class APIError(RuntimeError):
    pass


class RateLimit:
    """Global spacing of OKX calls; safely below the documented market API rate limit."""

    def __init__(self, interval: float = 0.14):
        self.interval = interval
        self.lock = threading.Lock()
        self.next_at = 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            pause = max(0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if pause:
            time.sleep(pause)


OKX_LIMIT = RateLimit()


def get_json(base: str, path: str, params: dict | None = None) -> dict:
    url = base + path + ("?" + urlencode(params) if params else "")
    last_error = None
    for attempt in range(4):
        try:
            req = Request(url, headers={"Accept": "application/json", "User-Agent": "OKX-BloFin-Hammer-Scanner/1.0"})
            with urlopen(req, timeout=18) as response:
                payload = json.load(response)
            if not isinstance(payload, dict):
                raise APIError("Unexpected JSON structure")
            if base in (OKX, BLOFIN) and str(payload.get("code")) != "0":
                raise APIError(f"API error: {payload.get('code')} {payload.get('msg')}")
            return payload
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, APIError) as err:
            last_error = err
            if attempt < 3:
                time.sleep(0.6 * (2 ** attempt) + random.random() * 0.3)
    raise APIError(f"GET failed after retries: {url} ({last_error})")


def marketcap_top400() -> dict[str, int]:
    """Ticker -> best market-cap rank. Do not silently substitute top traded assets."""
    ranks: dict[str, int] = {}
    for start in range(0, 400, 100):
        records = get_json(COINLORE, "/api/tickers/", {"start": start, "limit": 100}).get("data", [])
        if len(records) != 100:
            raise APIError(f"Market cap list incomplete: page starting {start} returned {len(records)} coins")
        for index, item in enumerate(records):
            symbol = str(item.get("symbol", "")).upper().strip()
            rank = int(item.get("rank") or (start + index + 1))
            if symbol and rank <= 400:
                ranks[symbol] = min(rank, ranks.get(symbol, rank))
    if len(ranks) < 350:
        raise APIError(f"Market cap symbols too few ({len(ranks)}): refusing to use a partial top-400 list")
    return ranks


def blofin_swaps() -> dict[str, dict]:
    rows = get_json(BLOFIN, "/api/v1/market/instruments").get("data", [])
    if not isinstance(rows, list) or not rows:
        raise APIError("BloFin returned no markets")
    result = {}
    for d in rows:
        if (d.get("state") != "live" or d.get("instType") != "SWAP"
                or d.get("quoteCurrency") != "USDT"
                or d.get("settleCurrency") != "USDT"
                or (d.get("assetClass") or "Crypto") != "Crypto"
                or (d.get("contractType") or "linear") != "linear"):
            continue
        if d.get("instId") != f"{d.get('baseCurrency')}-USDT":
            continue
        result[str(d["baseCurrency"]).upper()] = d
    if not result:
        raise APIError("No live USDT BloFin swap contracts")
    return result


def okx_swaps() -> dict[str, dict]:
    rows = get_json(OKX, "/api/v5/public/instruments", {"instType": "SWAP"}).get("data", [])
    if not isinstance(rows, list) or not rows:
        raise APIError("OKX returned no markets")
    result = {}
    for d in rows:
        if (d.get("state") != "live" or d.get("settleCcy") != "USDT"
                or d.get("ctType") != "linear"):
            continue
        symbol = str(d.get("instId", ""))
        if not symbol.endswith("-USDT-SWAP"):
            continue
        base = symbol.removesuffix("-USDT-SWAP")
        if base:
            result[base] = d
    if not result:
        raise APIError("No live OKX USDT perpetual swaps")
    return result


def eligible_markets(top: dict[str, int], blofin: dict, okx: dict) -> list[dict]:
    common = top.keys() & blofin.keys() & okx.keys()
    return [{"rank": top[s], "coin": s, "okx_inst_id": okx[s]["instId"],
             "blofin_inst_id": blofin[s]["instId"]} for s in sorted(common, key=lambda k: (top[k], k))]


@dataclass(frozen=True)
class Candle:
    ts: int
    o: float
    h: float
    l: float
    c: float


def closed_candles(inst_id: str) -> list[Candle]:
    OKX_LIMIT.wait()
    raw = get_json(OKX, "/api/v5/market/candles", {"instId": inst_id, "bar": "15m", "limit": 300}).get("data", [])
    if not isinstance(raw, list):
        raise APIError(f"Invalid candles for {inst_id}")
    result = []
    for row in raw:
        if len(row) < 9 or str(row[8]) != "1":
            continue  # skip candle still forming
        ts, o, h, l, c = int(row[0]), *[float(v) for v in row[1:5]]
        if l <= 0 or h <= l or min(o, c) < l or max(o, c) > h:
            continue
        result.append(Candle(ts, o, h, l, c))
    return sorted({x.ts: x for x in result}.values(), key=lambda x: x.ts)


def pivot_indices(candles: list[Candle], kind: str, radius: int = 2) -> list[int]:
    """Only *historically confirmed* swing pivots; current signal candle is excluded."""
    last = len(candles) - 1
    out: list[int] = []
    for i in range(radius, last - radius):
        value = candles[i].h if kind == "resistance" else candles[i].l
        neighbors = [candles[j].h if kind == "resistance" else candles[j].l
                     for j in range(i - radius, i + radius + 1) if j != i]
        if ((kind == "resistance" and value > max(neighbors))
                or (kind == "support" and value < min(neighbors))):
            out.append(i)
    return out


def find_prior_level(candles: list[Candle], kind: str, tolerance_pct: float,
                     min_touches: int, min_gap: int) -> tuple[float, int] | None:
    """Prior distinct swing tests in a narrow zone around the current wick extreme."""
    current = candles[-1]
    target = current.h if kind == "resistance" else current.l
    # Fixed relative width, no look-ahead and no use of the signal candle as a prior touch.
    tol = target * tolerance_pct
    pivots = pivot_indices(candles, kind)
    hits = sorted(((abs((candles[i].h if kind == "resistance" else candles[i].l) - target), i)
                   for i in pivots), key=lambda x: x[0])
    kept = []
    for distance, i in hits:
        if distance > tol:
            break
        if all(abs(i - j) >= min_gap for j in kept):
            kept.append(i)
    if len(kept) < min_touches:
        return None
    kept.sort()
    selected = [(candles[i].h if kind == "resistance" else candles[i].l) for i in kept]
    return mean(selected), len(kept)


def detect_signal(candles: list[Candle], tolerance_pct: float = 0.003,
                  min_touches: int = 2, min_gap: int = 4) -> dict | None:
    if len(candles) < MIN_CANDLES:
        return None
    x = candles[-1]
    extent = x.h - x.l
    if extent <= 0:
        return None
    body_low, body_high = min(x.o, x.c), max(x.o, x.c)
    # Support: Fibonacci 0 at HIGH, 0.382 is 38.2% DOWN from high.
    # Resistance: mirrored; Fibonacci 0 at LOW, 0.382 is 38.2% UP from low.
    # Body size is unrestricted provided it stays ENTIRELY inside the band.
    eps = extent * 1e-10
    if x.c > x.o and body_low >= x.h - 0.382 * extent - eps:
        kind, action = "support", "LONG_SETUP"
    elif x.c < x.o and body_high <= x.l + 0.382 * extent + eps:
        kind, action = "resistance", "SHORT_SETUP"
    else:
        return None
    level = find_prior_level(candles, kind, tolerance_pct, min_touches, min_gap)
    if level is None:
        return None
    return {
        "candle_time_utc": datetime.fromtimestamp(x.ts / 1000, timezone.utc).isoformat(),
        "signal": action, "level_type": kind,
        "prior_touches": level[1], "level_price": level[0],
        "open": x.o, "high": x.h, "low": x.l, "close": x.c,
        "fibonacci_band": "0-0.382", "timeframe": "15m",
    }


def write_csv(path: Path, rows: list[dict], fields: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def send_telegram(message: str):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not (token and chat_id):
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    req = Request(url, data=body, method="POST")
    with urlopen(req, timeout=20) as response:
        res = json.load(response)
    if not res.get("ok"):
        raise APIError("Telegram send failed")


def scan_one(market: dict, options) -> dict | None:
    bars = closed_candles(market["okx_inst_id"])
    if not bars:
        return None
    # If API is delayed or stale, do not report an old setup as newly actionable.
    age_ms = int(time.time() * 1000) - (bars[-1].ts + BAR_MS)
    if not (-BAR_MS <= age_ms <= 30 * 60 * 1000):
        return None
    signal = detect_signal(bars, options.zone_tolerance, options.min_touches, options.min_gap)
    return ({**market, **signal} if signal else None)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", default="output")
    p.add_argument("--list-only", action="store_true", help="Only export BloFin-tradable eligible symbols")
    p.add_argument("--max-markets", type=int, default=0, help="Optional local trial limit; 0 = all")
    p.add_argument("--zone-tolerance", type=float, default=0.003,
                   help="Wick extreme must be within this fractional distance of prior pivot levels (default 0.003=0.3%%)")
    p.add_argument("--min-touches", type=int, default=2)
    p.add_argument("--min-gap", type=int, default=4, help="Minimum 15m candles between distinct prior pivots")
    p.add_argument("--workers", type=int, default=6)
    args = p.parse_args()
    if not (0 < args.zone_tolerance <= 0.05 and args.min_touches >= 2 and args.min_gap >= 3 and 1 <= args.workers <= 12):
        p.error("Invalid tolerance, touches, gap, or workers")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    output = Path(args.output_dir)
    try:
        LOG.info("Fetching live OKX/BloFin instruments and top 400 market-cap coins")
        top = marketcap_top400()
        eligible = eligible_markets(top, blofin_swaps(), okx_swaps())
        if not eligible:
            raise APIError("No matching markets; aborting")
        fields = ["rank", "coin", "okx_inst_id", "blofin_inst_id"]
        write_csv(output / "blofin_okx_markets.csv", eligible, fields)
        LOG.info("Markets matched: %d (out of top 400)", len(eligible))
        if args.list_only:
            return 0
        markets = eligible[:args.max_markets] if args.max_markets > 0 else eligible
        signals = []
        failures = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(scan_one, m, args): m for m in markets}
            for fut in as_completed(futures):
                m = futures[fut]
                try:
                    sig = fut.result()
                    if sig:
                        signals.append(sig)
                except Exception as exc:
                    LOG.error("Market %s failed: %s", m["okx_inst_id"], exc)
                    failures.append(m["okx_inst_id"])
        if failures and len(failures) >= max(3, (len(markets) + 4) // 5):
            raise APIError(f"Too many candle API failures ({len(failures)}/{len(markets)}); aborting")
        signals.sort(key=lambda r: (r["rank"], r["coin"]))
        signal_fields = fields + ["candle_time_utc", "signal", "level_type", "prior_touches",
                                  "level_price", "open", "high", "low", "close", "fibonacci_band", "timeframe"]
        write_csv(output / "signals.csv", signals, signal_fields)
        report = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
                  "top_n": 400, "matched_markets": len(eligible), "scanned_markets": len(markets),
                  "failures": failures, "signals": len(signals), "data_source": "OKX",
                  "tradable_filter": "BloFin live USDT crypto perpetual swaps"}
        (output / "scan_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        LOG.info("Finished %d scans, %d signals, %d market errors", len(markets), len(signals), len(failures))
        for s in signals:
            line = (f"{s['signal']}: {s['coin']} | BloFin {s['blofin_inst_id']} | "
                    f"15m close {s['candle_time_utc']} | prior touches {s['prior_touches']} "
                    f"| level {s['level_price']:.8g}")
            print(line)
            try:
                send_telegram(line)
            except Exception as exc:
                LOG.error("Telegram delivery failed: %s", exc)
        return 0
    except APIError as exc:
        LOG.error("Scanner stopped: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
