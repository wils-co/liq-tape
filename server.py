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
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, parse_qs

HOST: str = "127.0.0.1"
DEFAULT_PORT: int = 8791
BASE_DIR: Path = Path(__file__).resolve().parent
DEFAULT_DATA_DIR: Path = BASE_DIR / "data"
INDEX_FILE: Path = BASE_DIR / "index.html"

# Lookback chips offered by the page, in seconds.
LOOKBACKS: Dict[str, int] = {"15m": 900, "1h": 3600, "4h": 14400}
DEFAULT_LOOKBACK: str = "1h"

# Coin path segment must be a plain ticker; keeps the data path un-traversable.
COIN_RE = re.compile(r"^[A-Z0-9]{1,16}$")

# Tail reading: walk backwards in chunks until the window is covered.
CHUNK_BYTES: int = 64 * 1024
MAX_TAIL_LINES: int = 4000

API_OI_RE = re.compile(r"^/api/oi/([^/]+)$")


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


class Handler(BaseHTTPRequestHandler):
    server_version = "liq-tape"
    sys_version = ""
    data_dir: Path = DEFAULT_DATA_DIR

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
        description="Serve the liq-tape quadrant panel over the local sampler files."
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
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    Handler.data_dir = Path(args.data_dir).resolve()

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
