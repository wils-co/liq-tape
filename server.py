#!/usr/bin/env python3
"""liq-tape local dashboard server.

Serves one static page plus a read-only JSON API over the sampler's
append-only jsonl output. Python standard library only: no dependencies,
no credentials, no outbound calls, no order path.

Binds loopback only. Read-only forever.
"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8791
BASE_DIR: Path = Path(__file__).resolve().parent
DEFAULT_DATA_DIR: Path = BASE_DIR / "data"
INDEX_FILE: Path = BASE_DIR / "index.html"
LEVELS_FILE: Path = BASE_DIR / "levels.yaml"

# The official read-only info client, invoked as a subprocess exactly as the
# sampler does. Same default location, overridable with --client.
DEFAULT_CLIENT_PATH: Path = (
    Path.home()
    / ".hermes"
    / "skills"
    / "blockchain"
    / "hyperliquid"
    / "scripts"
    / "hyperliquid_client.py"
)
CLIENT_TIMEOUT: float = 10.0

# L2 book: levels per side, and how long one fetch is reused. The panel polls
# every 2s; the cache means N open tabs still cost one client call per 2s.
L2_LEVELS: int = 15
L2_CACHE_TTL: float = 2.0

# Funding history: hourly entries over this window. The panel polls every 60s.
FUNDING_HOURS: int = 24
FUNDING_CACHE_TTL: float = 30.0

# Volume profile: 15m candles, bucketed by price. Bars are 15 minutes; a
# 60s cache is plenty — the next bar cannot exist yet.
PROFILE_LOOKBACKS: Dict[str, float] = {"4h": 4.0, "12h": 12.0, "24h": 24.0}
DEFAULT_PROFILE_LOOKBACK: str = "24h"
PROFILE_CACHE_TTL: float = 60.0
PROFILE_BUCKETS: int = 48
CANDLE_INTERVAL: str = "15m"

# Notable prints: last-N tape from the sampler, filtered by notional.
# 0.15% is the proximity used to attach a level's label to a print.
DEFAULT_PRINTS_LOOKBACK: str = "1h"
DEFAULT_MIN_NOTIONAL: float = 25000.0
PRINTS_CAP: int = 200
NEAR_LEVEL_FRAC: float = 0.0015
PRINTS_YOUNG_NOTE: str = (
    "prints need the sampler to accumulate — full picture in ~24h"
)

# A wall is an outlier on its own side of the book: 4× that side's median
# notional, floored at $1M so a quiet book is not a wall of walls.
WALL_MEDIAN_MULT: float = 4.0
WALL_FLOOR: float = 1_000_000.0

# Cumulative signed sampled tape. Same lookbacks as prints; the file is
# last-~10 prints per 12s poll, not a full tape — sampled: true says so.
DEFAULT_CVD_LOOKBACK: str = "1h"
CVD_SERIES_CAP: int = 240
CVD_YOUNG_NOTE: str = (
    "cvd needs the sampler to accumulate — sampled tape (last ~10/poll)"
)

# Lookback chips offered by the page, in seconds.
LOOKBACKS: Dict[str, int] = {"15m": 900, "1h": 3600, "4h": 14400}
DEFAULT_LOOKBACK: str = "1h"

# Coin path segment must be a plain ticker; keeps the data path un-traversable.
COIN_RE = re.compile(r"^[A-Z0-9]{1,16}$")

# Tail reading: walk backwards in chunks until the window is covered.
CHUNK_BYTES: int = 64 * 1024
MAX_TAIL_LINES: int = 4000
# Trades are denser than OI samples (~1.5–3k rows/hour). 80k lines covers a
# 24h window at the high end of that; window_capped is the honesty flag if
# a coin is hotter than the cap, not a reason to drop the cap.
MAX_TRADE_TAIL_LINES: int = 80000

API_OI_RE = re.compile(r"^/api/oi/([^/]+)$")
API_L2_RE = re.compile(r"^/api/l2/([^/]+)$")
API_FUNDING_RE = re.compile(r"^/api/funding/([^/]+)$")
API_PROFILE_RE = re.compile(r"^/api/profile/([^/]+)$")
API_PRINTS_RE = re.compile(r"^/api/prints/([^/]+)$")
API_CVD_RE = re.compile(r"^/api/cvd/([^/]+)$")

# levels.yaml is human-edited, so the reader understands one documented
# subset of YAML and names the line it could not read rather than guessing.
LEVEL_KINDS: Tuple[str, ...] = ("pool", "base", "session")
DEFAULT_KIND: str = "base"
LEVEL_COIN_RE = re.compile(r"^([A-Za-z0-9]{1,16}):$")
LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")


def read_recent_records(path: Path, cutoff_ts: float) -> Tuple[List[Dict[str, Any]], bool]:
    """Return parsed records from the tail of ``path``, newest last.

    Reads backwards from EOF in chunks and stops as soon as a record older
    than ``cutoff_ts`` is in hand (or the cap/file start is reached), so cost
    tracks the lookback window rather than total file growth.

    The bool is True when reading stopped at the cap while records older than
    the cutoff may still exist further back.
    """
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return [], False

        pos = size
        buf = b""
        capped = False
        complete: List[bytes] = []
        while True:
            step = min(CHUNK_BYTES, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf

            lines = buf.split(b"\n")
            # A chunk boundary can cut a line in half; drop the leading
            # fragment unless we reached the start of the file.
            complete = lines if pos == 0 else lines[1:]

            if pos == 0:
                break
            if len(complete) >= MAX_TAIL_LINES:
                capped = True
                break
            oldest_ts = _first_ts(complete)
            if oldest_ts is not None and oldest_ts < cutoff_ts:
                break

    records: List[Dict[str, Any]] = []
    for raw in complete:
        rec = _parse_line(raw)
        if rec is not None:
            records.append(rec)
    if len(records) > MAX_TAIL_LINES:
        records = records[-MAX_TAIL_LINES:]
        capped = True
    return records, capped


def _parse_line(raw: bytes) -> Optional[Dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    ts = rec.get("ts")
    if not isinstance(ts, (int, float)):
        return None
    return rec


def _first_ts(lines: List[bytes]) -> Optional[float]:
    for raw in lines:
        rec = _parse_line(raw)
        if rec is not None:
            return float(rec["ts"])
    return None


def _pct_delta(first: Optional[float], last: Optional[float]) -> Optional[float]:
    if first is None or last is None or first == 0:
        return None
    return (last - first) / first * 100.0


def build_snapshot(data_dir: Path, coin: str, lookback: str, now: float) -> Dict[str, Any]:
    """Assemble the JSON payload for one coin over one lookback window."""
    window = LOOKBACKS[lookback]
    cutoff = now - window
    path = data_dir / f"oi_{coin}.jsonl"

    records, capped = read_recent_records(path, cutoff)

    payload: Dict[str, Any] = {
        "coin": coin,
        "lookback": lookback,
        "lookback_seconds": window,
        "served_at": round(now, 3),
        "count": 0,
        "capped": capped,
        "latest": None,
        "first": None,
        "data_age": None,
        "coverage_seconds": None,
        "delta": {"mark_pct": None, "oi_pct": None},
    }

    if not records:
        payload["error"] = "no_data"
        payload["hint"] = "waiting for data — run sampler.py"
        return payload

    latest = records[-1]
    payload["latest"] = latest
    payload["data_age"] = round(now - float(latest["ts"]), 3)

    in_window = [r for r in records if float(r["ts"]) >= cutoff]
    payload["count"] = len(in_window)

    if len(in_window) < 2:
        # Newest sample is older than the window, or the window just opened.
        payload["error"] = "insufficient_window"
        payload["hint"] = "waiting for data — run sampler.py"
        return payload

    first = in_window[0]
    last = in_window[-1]
    payload["first"] = first
    payload["coverage_seconds"] = round(float(last["ts"]) - float(first["ts"]), 3)
    payload["delta"] = {
        "mark_pct": _pct_delta(first.get("mark"), last.get("mark")),
        "oi_pct": _pct_delta(first.get("oi"), last.get("oi")),
    }
    return payload


class ClientError(RuntimeError):
    """The official client could not be run, or did not return usable JSON."""


class TimedCache:
    """Per-key cache with a TTL and one in-flight producer per key.

    Two tabs polling the same coin share one subprocess call rather than
    racing two: the second waits on the key's lock and then finds the fresh
    value already stored.
    """

    def __init__(self, ttl: float) -> None:
        self._ttl = ttl
        self._values: Dict[str, Tuple[float, Any]] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _fresh(self, key: str) -> Optional[Any]:
        entry = self._values.get(key)
        if entry is not None and time.time() - entry[0] < self._ttl:
            return entry[1]
        return None

    def get(self, key: str, producer: Callable[[], Any]) -> Tuple[Any, bool]:
        """Return ``(value, cached)`` — calling ``producer`` only on a miss."""
        with self._guard:
            hit = self._fresh(key)
            if hit is not None:
                return hit, True
            lock = self._locks.setdefault(key, threading.Lock())

        with lock:
            with self._guard:
                hit = self._fresh(key)
            if hit is not None:
                return hit, True
            value = producer()
            with self._guard:
                self._values[key] = (time.time(), value)
            return value, False


L2_CACHE = TimedCache(L2_CACHE_TTL)
FUNDING_CACHE = TimedCache(FUNDING_CACHE_TTL)
PROFILE_CACHE = TimedCache(PROFILE_CACHE_TTL)


def run_client(client_path: Path, args: List[str]) -> Any:
    """Run the official client read-only and parse its --json output."""
    cmd = [sys.executable, str(client_path)] + args + ["--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=CLIENT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ClientError(f"client timed out after {CLIENT_TIMEOUT:g}s")
    except OSError as exc:
        raise ClientError(str(exc))

    if proc.returncode != 0:
        err = proc.stderr.strip().splitlines()
        raise ClientError(err[-1] if err else f"client exited with code {proc.returncode}")

    try:
        return json.loads(proc.stdout)
    except ValueError:
        raise ClientError("client returned non-JSON output")


def _to_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        number = float(val)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _level(raw: Any) -> Optional[Dict[str, Any]]:
    """One book level, px/sz converted from the client's strings to floats."""
    if not isinstance(raw, dict):
        return None
    px = _to_float(raw.get("px"))
    sz = _to_float(raw.get("sz"))
    if px is None or sz is None:
        return None
    orders = raw.get("orders")
    return {
        "px": px,
        "sz": sz,
        "orders": int(orders) if isinstance(orders, (int, float)) else None,
    }


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _tag_side_walls(levels: List[Dict[str, Any]]) -> float:
    """Add ``notional`` and ``wall`` on each level. Return this side's threshold.

    Threshold is 4× the median notional of *this* side, or ``WALL_FLOOR``,
    whichever is larger. One rule, computed per side so a thick bid book
    does not blank the asks (or the other way around).
    """
    notionals: List[float] = []
    for lvl in levels:
        notional = float(lvl["px"]) * float(lvl["sz"])
        lvl["notional"] = notional
        notionals.append(notional)
    if not notionals:
        return WALL_FLOOR
    threshold = max(WALL_MEDIAN_MULT * _median(notionals), WALL_FLOOR)
    for lvl in levels:
        lvl["wall"] = lvl["notional"] >= threshold
    return float(threshold)


def build_l2(client_path: Path, coin: str, now: float) -> Dict[str, Any]:
    """Assemble the JSON payload for one coin's L2 book."""
    raw = run_client(client_path, ["l2", coin, "--levels", str(L2_LEVELS)])
    if not isinstance(raw, dict):
        raise ClientError("unexpected l2 payload shape")

    bids = [lvl for lvl in map(_level, raw.get("bids") or []) if lvl][:L2_LEVELS]
    asks = [lvl for lvl in map(_level, raw.get("asks") or []) if lvl][:L2_LEVELS]
    bid_thr = _tag_side_walls(bids)
    ask_thr = _tag_side_walls(asks)

    book_time = raw.get("time")
    payload: Dict[str, Any] = {
        "coin": coin,
        "time": book_time if isinstance(book_time, (int, float)) else None,
        "served_at": round(now, 3),
        "levels": L2_LEVELS,
        "bids": bids,
        "asks": asks,
        "spread": None,
        "mid": None,
        "wall_threshold": {"bids": bid_thr, "asks": ask_thr},
    }

    if not bids or not asks:
        # One-sided or empty book: say so rather than publishing half a spread.
        payload["error"] = "no_book"
        payload["hint"] = f"no book returned for {coin}"
        return payload

    best_bid = bids[0]["px"]
    best_ask = asks[0]["px"]
    payload["spread"] = round(best_ask - best_bid, 8)
    payload["mid"] = round((best_ask + best_bid) / 2.0, 8)
    return payload


def build_funding(client_path: Path, coin: str, now: float) -> Dict[str, Any]:
    """Assemble the JSON payload for one coin's hourly funding history."""
    # --limit 0 is load-bearing: the client's display default is 20 rows, so a
    # 24h request silently returns 20 hours of history without it.
    raw = run_client(
        client_path, ["funding", coin, "--hours", str(FUNDING_HOURS), "--limit", "0"]
    )
    if not isinstance(raw, dict):
        raise ClientError("unexpected funding payload shape")

    hours: List[Dict[str, Any]] = []
    for item in raw.get("history") or []:
        if not isinstance(item, dict):
            continue
        stamp = item.get("time")
        funding = _to_float(item.get("funding_rate"))
        if not isinstance(stamp, (int, float)) or funding is None:
            continue
        hours.append(
            {
                "time": int(stamp),
                "funding": funding,
                "premium": _to_float(item.get("premium")),
            }
        )

    # The client returns newest first; the panel reads left-to-right in time.
    hours.sort(key=lambda h: h["time"])

    payload: Dict[str, Any] = {
        "coin": coin,
        "served_at": round(now, 3),
        "window_hours": FUNDING_HOURS,
        "count": len(hours),
        "hours": hours,
        "avg": None,
        "latest": None,
    }

    if not hours:
        payload["error"] = "no_funding"
        payload["hint"] = f"no funding history returned for {coin}"
        return payload

    payload["avg"] = sum(h["funding"] for h in hours) / len(hours)
    payload["latest"] = hours[-1]
    return payload


def _candle(raw: Any) -> Optional[Dict[str, Any]]:
    """One 15m bar, px/ohlc/v converted from the client's strings to floats."""
    if not isinstance(raw, dict):
        return None
    high = _to_float(raw.get("high", raw.get("h")))
    low = _to_float(raw.get("low", raw.get("l")))
    close = _to_float(raw.get("close", raw.get("c")))
    vol = _to_float(raw.get("volume", raw.get("v")))
    stamp = raw.get("time", raw.get("t"))
    if high is None or low is None or close is None or vol is None:
        return None
    if low > high:
        low, high = high, low
    return {
        "high": high,
        "low": low,
        "close": close,
        "vol": vol,
        "time": stamp if isinstance(stamp, (int, float)) else None,
    }


def _distribute_volume(
    buckets: List[Dict[str, float]], low: float, high: float, vol: float
) -> None:
    """Split one bar's volume uniformly across the buckets its low..high covers.

    A 15m candle has no intra-bar distribution; uniform is the honest
    approximation, not a tick profile.
    """
    if vol <= 0 or not buckets:
        return
    span = high - low
    if span <= 0:
        pmin = buckets[0]["lo"]
        pmax = buckets[-1]["hi"]
        width = pmax - pmin
        if width <= 0:
            buckets[0]["vol"] += vol
            return
        idx = int((low - pmin) / width * len(buckets))
        idx = min(len(buckets) - 1, max(0, idx))
        buckets[idx]["vol"] += vol
        return
    for bucket in buckets:
        overlap = min(high, bucket["hi"]) - max(low, bucket["lo"])
        if overlap > 0:
            bucket["vol"] += vol * (overlap / span)


def build_profile(
    client_path: Path, coin: str, lookback: str, now: float
) -> Dict[str, Any]:
    """Assemble the volume-at-price histogram and session VWAP for one window."""
    hours = PROFILE_LOOKBACKS[lookback]
    raw = run_client(
        client_path,
        [
            "candles",
            coin,
            "--interval",
            CANDLE_INTERVAL,
            "--hours",
            str(hours),
            "--limit",
            "0",
        ],
    )
    if not isinstance(raw, dict):
        raise ClientError("unexpected candles payload shape")

    candles = [c for c in (_candle(item) for item in (raw.get("candles") or [])) if c]

    window_ms = int(hours * 3600 * 1000)
    to_ms = int(now * 1000)
    from_ms = to_ms - window_ms

    payload: Dict[str, Any] = {
        "coin": coin,
        "lookback": lookback,
        "buckets": [],
        "vwap": None,
        "from": from_ms,
        "to": to_ms,
        "served_at": round(now, 3),
    }

    if not candles:
        payload["error"] = "no_candles"
        payload["hint"] = f"no {CANDLE_INTERVAL} candles returned for {coin}"
        return payload

    pmin = min(c["low"] for c in candles)
    pmax = max(c["high"] for c in candles)

    if pmax <= pmin:
        buckets: List[Dict[str, float]] = [{"lo": pmin, "hi": pmax, "vol": 0.0}]
    else:
        width = (pmax - pmin) / PROFILE_BUCKETS
        buckets = [
            {
                "lo": pmin + i * width,
                "hi": pmin + (i + 1) * width,
                "vol": 0.0,
            }
            for i in range(PROFILE_BUCKETS)
        ]
        buckets[-1]["hi"] = pmax

    for candle in candles:
        _distribute_volume(buckets, candle["low"], candle["high"], candle["vol"])

    # Typical-price VWAP over the window: Σ((h+l+c)/3 × v) / Σv.
    weighted = 0.0
    total_vol = 0.0
    for candle in candles:
        if candle["vol"] <= 0:
            continue
        typical = (candle["high"] + candle["low"] + candle["close"]) / 3.0
        weighted += typical * candle["vol"]
        total_vol += candle["vol"]

    payload["buckets"] = [
        {"lo": float(b["lo"]), "hi": float(b["hi"]), "vol": float(b["vol"])}
        for b in buckets
    ]
    payload["vwap"] = float(weighted / total_vol) if total_vol > 0 else None
    return payload


def _parse_trade_line(raw: bytes) -> Optional[Dict[str, Any]]:
    raw = raw.strip()
    if not raw:
        return None
    try:
        rec = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    tid = rec.get("tid")
    if tid is None or isinstance(tid, bool):
        return None
    try:
        rec = dict(rec)
        rec["tid"] = int(tid)
    except (TypeError, ValueError):
        return None
    stamp = rec.get("time")
    if not isinstance(stamp, (int, float)):
        return None
    return rec


def _first_trade_time(lines: List[bytes]) -> Optional[float]:
    for raw in lines:
        rec = _parse_trade_line(raw)
        if rec is not None:
            return float(rec["time"])
    return None


def read_recent_trades(
    path: Path,
    cutoff_ms: float,
    max_lines: Optional[int] = None,
) -> Tuple[List[Dict[str, Any]], bool]:
    """Return parsed trades from the tail of ``path``, oldest first.

    ``time`` is milliseconds. Same backwards-chunk walk as the OI reader, so
    cost tracks the lookback rather than total file growth.

    The bool is True only when the line cap is what cut the window short —
    the oldest remaining row is still inside the lookback. Hitting the cap
    on older-than-window rows is not a truncated window.
    """
    if max_lines is None:
        max_lines = MAX_TRADE_TAIL_LINES
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        if size == 0:
            return [], False

        pos = size
        buf = b""
        capped = False
        complete: List[bytes] = []
        while True:
            step = min(CHUNK_BYTES, pos)
            pos -= step
            f.seek(pos)
            buf = f.read(step) + buf

            lines = buf.split(b"\n")
            complete = lines if pos == 0 else lines[1:]

            if pos == 0:
                break
            if len(complete) >= max_lines:
                capped = True
                break
            oldest = _first_trade_time(complete)
            if oldest is not None and oldest < cutoff_ms:
                break

    records: List[Dict[str, Any]] = []
    for raw in complete:
        rec = _parse_trade_line(raw)
        if rec is not None:
            records.append(rec)
    if len(records) > max_lines:
        records = records[-max_lines:]
        capped = True
    if capped and records and float(records[0]["time"]) < cutoff_ms:
        capped = False
    return records, capped


def _usable_order_hash(rec: Dict[str, Any]) -> Optional[str]:
    """Taker-order id, or None when the field cannot group fills.

    Hyperliquid often ships ``hash`` as 0x000… for fills that are not a
    signed taker order. Collapsing those into one print would invent a
    sweep that did not happen.
    """
    raw = rec.get("hash")
    if not isinstance(raw, str) or not raw:
        return None
    body = raw[2:] if raw.startswith(("0x", "0X")) else raw
    if not body or set(body) <= {"0"}:
        return None
    return raw


def _grouped_fills(records: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group in-window fills that share a usable taker-order hash and side.

    One Hyperliquid action can carry both sides. Hash-only grouping would
    fuse a bid-taker fill and an ask-taker fill into one print at an
    average price — inventing a sweep that did not happen. A sweep is
    one-sided; mixed batches split.
    """
    groups: Dict[Any, List[Dict[str, Any]]] = {}
    order: List[Any] = []
    for rec in records:
        digest = _usable_order_hash(rec)
        if digest is None:
            key: Any = ("tid", rec["tid"])
        else:
            key = (digest, rec.get("side"))
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(rec)
    return [groups[k] for k in order]


def _fills_to_print(
    fills: List[Dict[str, Any]], levels: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """One print from one or more fills. sz summed, px is the size-weighted mean."""
    sz_sum = 0.0
    notional_sum = 0.0
    times: List[int] = []
    sides: List[str] = []
    for rec in fills:
        px = _to_float(rec.get("px"))
        sz = _to_float(rec.get("sz"))
        if px is None or sz is None:
            continue
        sz_sum += sz
        notional_sum += px * sz
        times.append(int(rec["time"]))
        side = rec.get("side")
        if side in ("B", "A"):
            sides.append(side)
    if not times or sz_sum <= 0:
        return None
    px = notional_sum / sz_sum
    uniq = set(sides)
    side = next(iter(uniq)) if len(uniq) == 1 else None
    return {
        "px": float(px),
        "sz": float(sz_sum),
        "notional": float(notional_sum),
        "side": side,
        "time": max(times),
        "near": _near_labels(px, levels),
    }


def _near_labels(px: float, levels: List[Dict[str, Any]]) -> List[str]:
    """Labels of structure levels within 0.15% of ``px``. Empty if none."""
    labels: List[str] = []
    for lvl in levels:
        price = lvl.get("price")
        if not isinstance(price, (int, float)) or price == 0:
            continue
        if abs(px - float(price)) / abs(float(price)) <= NEAR_LEVEL_FRAC:
            label = lvl.get("label")
            labels.append(label if isinstance(label, str) and label else str(price))
    return labels


def build_prints(
    data_dir: Path,
    levels_file: Path,
    coin: str,
    lookback: str,
    min_notional: float,
    now: float,
    max_lines: Optional[int] = None,
) -> Dict[str, Any]:
    """Assemble notable prints for one coin over one lookback window."""
    window = LOOKBACKS[lookback]
    cutoff_ms = (now - window) * 1000.0
    path = data_dir / f"trades_{coin}.jsonl"

    payload: Dict[str, Any] = {
        "coin": coin,
        "lookback": lookback,
        "min_notional": float(min_notional),
        "prints": [],
        "count": 0,
        "truncated": False,
        "window_capped": False,
        "served_at": round(now, 3),
    }

    if not path.is_file():
        payload["note"] = PRINTS_YOUNG_NOTE
        return payload

    records, window_capped = read_recent_trades(path, cutoff_ms, max_lines=max_lines)
    payload["window_capped"] = window_capped

    by_tid: Dict[int, Dict[str, Any]] = {}
    for rec in records:
        by_tid[rec["tid"]] = rec

    coin_levels: List[Dict[str, Any]] = []
    if levels_file.is_file():
        try:
            coins, _errors = parse_levels(levels_file.read_text(encoding="utf-8"))
            coin_levels = coins.get(coin, [])
        except (OSError, UnicodeDecodeError):
            coin_levels = []

    in_window = [rec for rec in by_tid.values() if float(rec["time"]) >= cutoff_ms]
    out: List[Dict[str, Any]] = []
    for fills in _grouped_fills(in_window):
        row = _fills_to_print(fills, coin_levels)
        if row is None or row["notional"] < min_notional:
            continue
        out.append(row)

    out.sort(key=lambda row: row["time"], reverse=True)
    truncated = len(out) > PRINTS_CAP or window_capped
    out = out[:PRINTS_CAP]
    payload["prints"] = out
    payload["count"] = len(out)
    payload["truncated"] = truncated
    if not out:
        payload["note"] = PRINTS_YOUNG_NOTE
    return payload


def _downsample_series(
    points: List[Dict[str, Any]], cap: int
) -> List[Dict[str, Any]]:
    """Keep first and last; stride the middle so a spark is not 80k points.

    Latest CVD is computed over every in-window fill; this only thins the
    series the page draws. Duplicate indices from rounding are dropped.
    """
    if cap <= 0 or len(points) <= cap:
        return points
    if cap == 1:
        return [points[-1]]
    out: List[Dict[str, Any]] = []
    last_idx: Optional[int] = None
    n = len(points)
    for i in range(cap):
        idx = int(round(i * (n - 1) / (cap - 1)))
        if idx == last_idx:
            continue
        out.append(points[idx])
        last_idx = idx
    if out[-1]["time"] != points[-1]["time"]:
        out[-1] = points[-1]
    return out


def build_cvd(
    data_dir: Path,
    coin: str,
    lookback: str,
    now: float,
    max_lines: Optional[int] = None,
) -> Dict[str, Any]:
    """Cumulative signed notional of the sampled tape over one lookback.

    Side ``B`` adds ``px*sz``, side ``A`` subtracts. px/sz arrive as strings
    on the jsonl and leave as floats. Missing file is empty + note, not 404.
    """
    window = LOOKBACKS[lookback]
    cutoff_ms = (now - window) * 1000.0
    path = data_dir / f"trades_{coin}.jsonl"

    payload: Dict[str, Any] = {
        "coin": coin,
        "lookback": lookback,
        "sampled": True,
        "cvd": {"latest": None, "series": []},
        "count": 0,
        "truncated": False,
        "window_capped": False,
        "served_at": round(now, 3),
    }

    if not path.is_file():
        payload["note"] = CVD_YOUNG_NOTE
        return payload

    records, window_capped = read_recent_trades(path, cutoff_ms, max_lines=max_lines)
    payload["window_capped"] = window_capped

    by_tid: Dict[int, Dict[str, Any]] = {}
    for rec in records:
        by_tid[rec["tid"]] = rec

    in_window = [rec for rec in by_tid.values() if float(rec["time"]) >= cutoff_ms]
    in_window.sort(key=lambda rec: (float(rec["time"]), rec["tid"]))

    running = 0.0
    series: List[Dict[str, Any]] = []
    for rec in in_window:
        px = _to_float(rec.get("px"))
        sz = _to_float(rec.get("sz"))
        side = rec.get("side")
        if px is None or sz is None or side not in ("B", "A"):
            continue
        running += px * sz if side == "B" else -(px * sz)
        series.append({"time": int(rec["time"]), "cvd": running})

    payload["count"] = len(series)
    payload["truncated"] = window_capped
    if not series:
        payload["note"] = CVD_YOUNG_NOTE
        return payload

    payload["cvd"] = {
        "latest": running,
        "series": _downsample_series(series, CVD_SERIES_CAP),
    }
    return payload


def _strip_comment(line: str) -> str:
    """Drop a trailing ``#`` comment, ignoring ``#`` inside a quoted string."""
    quote = ""
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            return line[:i]
    return line


def _split_top(text: str, sep: str) -> List[str]:
    """Split on ``sep`` at quote depth zero, so labels may contain it."""
    parts: List[str] = []
    quote = ""
    buf = ""
    for ch in text:
        if quote:
            buf += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf += ch
            continue
        if ch == sep:
            parts.append(buf)
            buf = ""
            continue
        buf += ch
    parts.append(buf)
    return parts


def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _flow_mapping(body: str) -> Optional[Dict[str, str]]:
    """Parse ``{price: 1, label: "x", kind: pool}`` into a string mapping."""
    out: Dict[str, str] = {}
    for chunk in _split_top(body, ","):
        chunk = chunk.strip()
        if not chunk:
            continue
        head = _split_top(chunk, ":")
        if len(head) < 2:
            return None
        key = head[0].strip()
        if not key:
            return None
        out[key] = ":".join(head[1:]).strip()
    return out


def _level_entry(fields: Dict[str, str], lineno: int) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate one parsed entry. Returns ``(level, error)`` — never both."""
    raw_price = fields.get("price")
    if raw_price is None:
        return None, "entry has no price"
    price = _to_float(_unquote(raw_price))
    if price is None:
        return None, f"price {raw_price!r} is not a number"

    kind = _unquote(fields.get("kind", DEFAULT_KIND)) or DEFAULT_KIND
    if kind not in LEVEL_KINDS:
        return None, f"kind {kind!r} is not one of {'|'.join(LEVEL_KINDS)}"

    return {
        "price": price,
        "label": _unquote(fields.get("label", "")),
        "kind": kind,
        "line": lineno,
    }, None


def parse_levels(text: str) -> Tuple[Dict[str, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Read the documented levels.yaml subset. Returns ``(coins, errors)``.

    Deliberately not a YAML implementation: it understands ``levels:``, one
    indented coin key per block, and ``- {price: .., label: .., kind: ..}``
    entries (or the same keys on indented lines under a bare ``-``). Anything
    it cannot read becomes an error naming the line, so a typo in a
    hand-edited file is visible rather than a silently missing line.
    """
    coins: Dict[str, List[Dict[str, Any]]] = {}
    errors: List[Dict[str, Any]] = []

    def fail(lineno: int, detail: str) -> None:
        errors.append({"line": lineno, "detail": detail})

    in_levels = False
    coin: Optional[str] = None
    # An open block-style entry: its fields, its line, and the indent of the
    # "- " that opened it. Continuation lines must be indented past that.
    pending: Optional[Dict[str, Any]] = None

    def close_pending() -> None:
        nonlocal pending
        if pending is None:
            return
        level, err = _level_entry(pending["fields"], pending["line"])
        if err:
            fail(pending["line"], err)
        elif pending["coin"] is not None:
            coins.setdefault(pending["coin"], []).append(level)
        pending = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        body = _strip_comment(raw).rstrip()
        if not body.strip():
            continue
        indent = len(body) - len(body.lstrip())
        stripped = body.strip()

        if pending is not None and not (indent > pending["indent"] and not stripped.startswith("- ")):
            close_pending()

        if indent == 0:
            if stripped == "levels:":
                in_levels = True
                coin = None
            else:
                fail(lineno, f"ignored top-level line {stripped!r} (expected 'levels:')")
                in_levels = False
                coin = None
            continue

        if not in_levels:
            fail(lineno, "line sits outside a 'levels:' block")
            continue

        if stripped.startswith("- "):
            if coin is None:
                fail(lineno, "entry before any coin heading")
                continue
            rest = stripped[2:].strip()
            if rest.startswith("{") and rest.endswith("}"):
                fields = _flow_mapping(rest[1:-1])
                if fields is None:
                    fail(lineno, "could not read the {price: .., label: .., kind: ..} entry")
                    continue
                level, err = _level_entry(fields, lineno)
                if err:
                    fail(lineno, err)
                else:
                    coins.setdefault(coin, []).append(level)
                continue
            # Block style: this line opens an entry, deeper lines add to it.
            pending = {"fields": {}, "line": lineno, "indent": indent, "coin": coin}
            if rest:
                match = LEVEL_KEY_RE.match(rest)
                if not match:
                    fail(lineno, f"could not read {rest!r} as 'key: value'")
                    pending = None
                    continue
                pending["fields"][match.group(1)] = match.group(2).strip()
            continue

        if pending is not None:
            match = LEVEL_KEY_RE.match(stripped)
            if not match:
                fail(lineno, f"could not read {stripped!r} as 'key: value'")
                continue
            pending["fields"][match.group(1)] = match.group(2).strip()
            continue

        match = LEVEL_COIN_RE.match(stripped)
        if match:
            coin = match.group(1).upper()
            coins.setdefault(coin, [])
            continue

        fail(lineno, f"could not read {stripped!r} as a coin heading or an entry")

    close_pending()

    # Highest price first: the page draws top-down and reads the same way.
    for entries in coins.values():
        entries.sort(key=lambda lvl: lvl["price"], reverse=True)
    return coins, errors


def build_levels(path: Path, now: float) -> Dict[str, Any]:
    """Assemble the JSON payload for the whole levels file.

    Read per request, never cached: the point of a hand-edited file is that a
    save plus a refresh is the whole edit loop.
    """
    coins, errors = parse_levels(path.read_text(encoding="utf-8"))
    return {
        "served_at": round(now, 3),
        "source": path.name,
        "kinds": list(LEVEL_KINDS),
        "coins": coins,
        "count": sum(len(v) for v in coins.values()),
        "errors": errors,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "liq-tape"
    sys_version = ""
    data_dir: Path = DEFAULT_DATA_DIR
    client_path: Path = DEFAULT_CLIENT_PATH
    levels_file: Path = LEVELS_FILE

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        parsed = urlparse(self.path)
        route = parsed.path

        if route in ("/", "/index.html"):
            self._send_index()
            return
        if route == "/favicon.ico":
            self.send_response(HTTPStatus.NO_CONTENT)
            self.end_headers()
            return

        if route == "/api/levels":
            self._send_levels()
            return

        match = API_OI_RE.match(route)
        if match:
            self._send_oi(match.group(1), parse_qs(parsed.query))
            return

        match = API_L2_RE.match(route)
        if match:
            self._send_live(match.group(1), "l2")
            return

        match = API_FUNDING_RE.match(route)
        if match:
            self._send_live(match.group(1), "funding")
            return

        match = API_PROFILE_RE.match(route)
        if match:
            self._send_profile(match.group(1), parse_qs(parsed.query))
            return

        match = API_PRINTS_RE.match(route)
        if match:
            self._send_prints(match.group(1), parse_qs(parsed.query))
            return

        match = API_CVD_RE.match(route)
        if match:
            self._send_cvd(match.group(1), parse_qs(parsed.query))
            return

        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not_found", "path": route})

    def _send_index(self) -> None:
        try:
            body = INDEX_FILE.read_bytes()
        except OSError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "index_missing", "expected": str(INDEX_FILE)},
            )
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_levels(self) -> None:
        """Serve the hand-edited levels file. This endpoint only ever reads."""
        if not self.levels_file.is_file():
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "no_levels_file",
                    "expected": str(self.levels_file),
                    "hint": "no levels.yaml — create one to draw your levels",
                },
            )
            return

        try:
            payload = build_levels(self.levels_file, time.time())
        except OSError as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "read_failed", "detail": str(exc)},
            )
            return
        except UnicodeDecodeError:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "read_failed", "detail": "levels.yaml is not UTF-8 text"},
            )
            return

        self._send_json(HTTPStatus.OK, payload)

    def _resolve_coin(self, raw_coin: str) -> Optional[str]:
        """Return the tracked coin for this path segment, or None (404 sent).

        Gating every endpoint on "the sampler has a file for it" keeps the
        DOGE/404 shape the page already handles, and means a loopback request
        can only ever spawn a client call for a coin this box already tracks.
        """
        coin = raw_coin.upper()
        if not COIN_RE.match(coin):
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "unknown_coin", "coin": raw_coin}
            )
            return None
        if not (self.data_dir / f"oi_{coin}.jsonl").is_file():
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "unknown_coin",
                    "coin": coin,
                    "hint": "no sampler file for this coin",
                },
            )
            return None
        return coin

    def _send_live(self, raw_coin: str, kind: str) -> None:
        """Serve one of the client-backed endpoints (l2 or funding)."""
        coin = self._resolve_coin(raw_coin)
        if coin is None:
            return

        if not self.client_path.is_file():
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": "client_missing",
                    "coin": coin,
                    "expected": str(self.client_path),
                    "hint": "point --client at the official client",
                },
            )
            return

        cache = L2_CACHE if kind == "l2" else FUNDING_CACHE
        builder = build_l2 if kind == "l2" else build_funding
        try:
            payload, cached = cache.get(
                coin, lambda: builder(self.client_path, coin, time.time())
            )
        except ClientError as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "client_failed",
                    "coin": coin,
                    "detail": str(exc),
                    "hint": "the official client did not return data",
                },
            )
            return

        payload = dict(payload)
        payload["cached"] = cached
        self._send_json(HTTPStatus.OK, payload)

    def _send_profile(self, raw_coin: str, query: Dict[str, List[str]]) -> None:
        coin = self._resolve_coin(raw_coin)
        if coin is None:
            return

        lookback = (query.get("lookback") or [DEFAULT_PROFILE_LOOKBACK])[0]
        if lookback not in PROFILE_LOOKBACKS:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "bad_lookback",
                    "lookback": lookback,
                    "allowed": list(PROFILE_LOOKBACKS),
                },
            )
            return

        if not self.client_path.is_file():
            self._send_json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {
                    "error": "client_missing",
                    "coin": coin,
                    "expected": str(self.client_path),
                    "hint": "point --client at the official client",
                },
            )
            return

        try:
            payload, cached = PROFILE_CACHE.get(
                f"{coin}:{lookback}",
                lambda: build_profile(self.client_path, coin, lookback, time.time()),
            )
        except ClientError as exc:
            self._send_json(
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "client_failed",
                    "coin": coin,
                    "detail": str(exc),
                    "hint": "the official client did not return data",
                },
            )
            return

        payload = dict(payload)
        payload["cached"] = cached
        self._send_json(HTTPStatus.OK, payload)

    def _send_prints(self, raw_coin: str, query: Dict[str, List[str]]) -> None:
        coin = self._resolve_coin(raw_coin)
        if coin is None:
            return

        lookback = (query.get("lookback") or [DEFAULT_PRINTS_LOOKBACK])[0]
        if lookback not in LOOKBACKS:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "bad_lookback",
                    "lookback": lookback,
                    "allowed": list(LOOKBACKS),
                },
            )
            return

        raw_min = (query.get("min_notional") or [str(DEFAULT_MIN_NOTIONAL)])[0]
        min_notional = _to_float(raw_min)
        if min_notional is None or min_notional < 0:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "bad_min_notional",
                    "min_notional": raw_min,
                },
            )
            return

        try:
            payload = build_prints(
                self.data_dir,
                self.levels_file,
                coin,
                lookback,
                min_notional,
                time.time(),
            )
        except OSError as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "read_failed", "coin": coin, "detail": str(exc)},
            )
            return

        self._send_json(HTTPStatus.OK, payload)

    def _send_cvd(self, raw_coin: str, query: Dict[str, List[str]]) -> None:
        coin = self._resolve_coin(raw_coin)
        if coin is None:
            return

        lookback = (query.get("lookback") or [DEFAULT_CVD_LOOKBACK])[0]
        if lookback not in LOOKBACKS:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "bad_lookback",
                    "lookback": lookback,
                    "allowed": list(LOOKBACKS),
                },
            )
            return

        try:
            payload = build_cvd(self.data_dir, coin, lookback, time.time())
        except OSError as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "read_failed", "coin": coin, "detail": str(exc)},
            )
            return

        self._send_json(HTTPStatus.OK, payload)

    def _send_oi(self, raw_coin: str, query: Dict[str, List[str]]) -> None:
        coin = raw_coin.upper()
        if not COIN_RE.match(coin):
            self._send_json(
                HTTPStatus.NOT_FOUND, {"error": "unknown_coin", "coin": raw_coin}
            )
            return

        lookback = (query.get("lookback") or [DEFAULT_LOOKBACK])[0]
        if lookback not in LOOKBACKS:
            self._send_json(
                HTTPStatus.BAD_REQUEST,
                {
                    "error": "bad_lookback",
                    "lookback": lookback,
                    "allowed": list(LOOKBACKS),
                },
            )
            return

        path = self.data_dir / f"oi_{coin}.jsonl"
        if not path.is_file():
            self._send_json(
                HTTPStatus.NOT_FOUND,
                {
                    "error": "unknown_coin",
                    "coin": coin,
                    "hint": "no sampler file for this coin",
                },
            )
            return

        try:
            payload = build_snapshot(self.data_dir, coin, lookback, time.time())
        except OSError as exc:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": "read_failed", "coin": coin, "detail": str(exc)},
            )
            return

        self._send_json(HTTPStatus.OK, payload)

    def _send_json(self, status: HTTPStatus, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Serve the liq-tape panels over the local sampler files and the "
            "official read-only client."
        )
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=DEFAULT_PORT,
        help=f"Loopback port to listen on (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--data-dir",
        "-d",
        type=str,
        default=str(DEFAULT_DATA_DIR),
        help=f"Directory holding oi_<COIN>.jsonl (default: {DEFAULT_DATA_DIR})",
    )
    parser.add_argument(
        "--levels",
        type=str,
        default=str(LEVELS_FILE),
        help=f"Path to the hand-edited levels file (default: {LEVELS_FILE})",
    )
    parser.add_argument(
        "--client",
        type=str,
        default=str(DEFAULT_CLIENT_PATH),
        help=f"Path to hyperliquid_client.py (default: {DEFAULT_CLIENT_PATH})",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    Handler.data_dir = Path(args.data_dir).resolve()
    Handler.client_path = Path(args.client).resolve()
    Handler.levels_file = Path(args.levels).resolve()

    if not Handler.client_path.is_file():
        # Not fatal: the OI panel reads files only. The client-backed panels
        # say so per request rather than the whole server refusing to start.
        print(
            f"warning: client not found at {Handler.client_path} — "
            "L2, funding, and profile panels will report client_missing",
            flush=True,
        )

    httpd = ThreadingHTTPServer((HOST, args.port), Handler)
    print(
        f"liq-tape server on http://{HOST}:{args.port} | data: {Handler.data_dir}",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
