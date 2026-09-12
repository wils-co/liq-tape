#!/usr/bin/env python3
"""liq-tape local dashboard server.

Serves one static page plus a read-only JSON API over the sampler's
append-only jsonl output. Python standard library only: no dependencies,
no credentials, no outbound calls, no order path.

Binds loopback only. Read-only forever.
"""

import argparse
import json
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

# Lookback chips offered by the page, in seconds.
LOOKBACKS: Dict[str, int] = {"15m": 900, "1h": 3600, "4h": 14400}
DEFAULT_LOOKBACK: str = "1h"

# Coin path segment must be a plain ticker; keeps the data path un-traversable.
COIN_RE = re.compile(r"^[A-Z0-9]{1,16}$")

# Tail reading: walk backwards in chunks until the window is covered.
CHUNK_BYTES: int = 64 * 1024
MAX_TAIL_LINES: int = 4000

API_OI_RE = re.compile(r"^/api/oi/([^/]+)$")
API_L2_RE = re.compile(r"^/api/l2/([^/]+)$")
API_FUNDING_RE = re.compile(r"^/api/funding/([^/]+)$")


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
        return float(val)
    except (TypeError, ValueError):
        return None


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


def build_l2(client_path: Path, coin: str, now: float) -> Dict[str, Any]:
    """Assemble the JSON payload for one coin's L2 book."""
    raw = run_client(client_path, ["l2", coin, "--levels", str(L2_LEVELS)])
    if not isinstance(raw, dict):
        raise ClientError("unexpected l2 payload shape")

    bids = [lvl for lvl in map(_level, raw.get("bids") or []) if lvl][:L2_LEVELS]
    asks = [lvl for lvl in map(_level, raw.get("asks") or []) if lvl][:L2_LEVELS]

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


class Handler(BaseHTTPRequestHandler):
    server_version = "liq-tape"
    sys_version = ""
    data_dir: Path = DEFAULT_DATA_DIR
    client_path: Path = DEFAULT_CLIENT_PATH

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

    if not Handler.client_path.is_file():
        # Not fatal: the OI panel reads files only. The client-backed panels
        # say so per request rather than the whole server refusing to start.
        print(
            f"warning: client not found at {Handler.client_path} — "
            "L2 and funding panels will report client_missing",
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
