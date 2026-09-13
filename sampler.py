#!/usr/bin/env python3
"""liq-tape OI, mark, and recent-trades sampler.

Polls the official Hyperliquid client helper for perpetual market contexts
and the last handful of trades, appending to per-coin jsonl files.
Read-only: no order execution, no credentials, no signal generation.
"""

import argparse
from collections import deque
import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Deque, Dict, List, Optional, Set

DEFAULT_INTERVAL: float = 12.0
DEFAULT_COINS: List[str] = ["BTC", "ETH", "HYPE", "SOL"]
BASE_DIR: Path = Path(__file__).resolve().parent
DEFAULT_DATA_DIR: Path = BASE_DIR / "data"
# recentTrades only returns the last ~10 prints; keep a bounded memory of
# tids so overlapping polls and a restart against the file tail do not
# re-append the same row.
TID_CAP: int = 2000
TID_TAIL_BYTES: int = 256 * 1024
TRADES_TIMEOUT: float = 8.0
DEFAULT_CLIENT_PATH: Path = (
    Path.home()
    / ".hermes"
    / "skills"
    / "blockchain"
    / "hyperliquid"
    / "scripts"
    / "hyperliquid_client.py"
)

running: bool = True


def _handle_signal(signum: int, frame: Any) -> None:
    global running
    running = False


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def _to_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


class TidGate:
    """Bounded set of recently seen trade ids. Oldest dropped first."""

    def __init__(self, cap: int = TID_CAP) -> None:
        self._cap = cap
        self._order: Deque[int] = deque()
        self._seen: Set[int] = set()

    def add(self, tid: int) -> bool:
        """Record ``tid``. Return True only the first time it is seen."""
        if tid in self._seen:
            return False
        self._seen.add(tid)
        self._order.append(tid)
        while len(self._order) > self._cap:
            old = self._order.popleft()
            self._seen.discard(old)
        return True


def _trade_tid(rec: Dict[str, Any]) -> Optional[int]:
    raw = rec.get("tid")
    if raw is None or isinstance(raw, bool):
        return None
    try:
        tid = int(raw)
    except (TypeError, ValueError):
        return None
    return tid


def load_tid_gate(path: Path) -> TidGate:
    """Resume the watermark from the tail of an existing trades file.

    The file does not have to exist. Partial lines at a chunk boundary are
    dropped. Older rows further back are not re-appended: recentTrades only
    ever returns the last ~10 prints, so the tail is enough.
    """
    gate = TidGate()
    if not path.is_file():
        return gate
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            take = min(size, TID_TAIL_BYTES)
            f.seek(size - take)
            raw = f.read()
    except OSError:
        return gate
    if size > take:
        nl = raw.find(b"\n")
        if nl >= 0:
            raw = raw[nl + 1 :]
        else:
            raw = b""
    for line in raw.split(b"\n"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        tid = _trade_tid(rec)
        if tid is not None:
            gate.add(tid)
    return gate


def fetch_recent_trades(client_path: Path, coin: str) -> List[Dict[str, Any]]:
    cmd = [
        sys.executable,
        str(client_path),
        "trades",
        coin,
        "--json",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=TRADES_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"trades client exited with code {proc.returncode}: {proc.stderr.strip()}"
        )
    payload = json.loads(proc.stdout)
    if isinstance(payload, list):
        rows = payload
    elif isinstance(payload, dict):
        rows = payload.get("trades") or []
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def fetch_markets(client_path: Path) -> Dict[str, Any]:
    cmd = [
        sys.executable,
        str(client_path),
        "markets",
        "--limit",
        "0",
        "--json",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Client exited with code {proc.returncode}: {proc.stderr.strip()}"
        )
    return json.loads(proc.stdout)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll mark price, open interest, funding, and premium into JSONL files."
    )
    parser.add_argument(
        "--interval",
        "-i",
        type=float,
        default=DEFAULT_INTERVAL,
        help=f"Polling interval in seconds (default: {DEFAULT_INTERVAL})",
    )
    parser.add_argument(
        "--coins",
        "-c",
        type=str,
        default=",".join(DEFAULT_COINS),
        help=f"Comma-separated coins to track (default: {','.join(DEFAULT_COINS)})",
    )
    parser.add_argument(
        "--data-dir",
        "-d",
        type=str,
        default=str(DEFAULT_DATA_DIR),
        help=f"Path to data directory (default: {DEFAULT_DATA_DIR})",
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
    interval: float = max(1.0, args.interval)
    coins: List[str] = [c.strip().upper() for c in args.coins.split(",") if c.strip()]
    data_dir: Path = Path(args.data_dir).resolve()
    client_path: Path = Path(args.client).resolve()

    if not client_path.is_file():
        sys.stderr.write(f"Error: client script not found at {client_path}\n")
        sys.exit(1)

    data_dir.mkdir(parents=True, exist_ok=True)

    tid_gates: Dict[str, TidGate] = {
        coin: load_tid_gate(data_dir / f"trades_{coin}.jsonl") for coin in coins
    }

    # Required single startup line
    print(
        f"liq-tape sampler started | coins: {', '.join(coins)} | interval: {interval}s | data: {data_dir}",
        flush=True,
    )

    while running:
        loop_start = time.time()
        try:
            payload = fetch_markets(client_path)
            raw_markets = payload.get("markets", [])
            lookup: Dict[str, Dict[str, Any]] = {
                m.get("coin"): m for m in raw_markets if isinstance(m, dict)
            }
            sample_ts = round(time.time(), 3)

            for coin in coins:
                m = lookup.get(coin)
                if not m:
                    now_str = datetime.datetime.now().isoformat()
                    sys.stderr.write(
                        f"[{now_str}] Warning: coin {coin} missing in markets data\n"
                    )
                    sys.stderr.flush()
                    continue

                record = {
                    "ts": sample_ts,
                    "mark": _to_float(m.get("mark_px")),
                    "oi": _to_float(m.get("open_interest")),
                    "funding": _to_float(m.get("funding")),
                    "premium": _to_float(m.get("premium")),
                }

                target_file = data_dir / f"oi_{coin}.jsonl"
                line = json.dumps(record) + "\n"
                with open(target_file, "a", encoding="utf-8") as f:
                    f.write(line)

                # A failed trades poll must never skip the OI write above.
                try:
                    trades = fetch_recent_trades(client_path, coin)
                    gate = tid_gates[coin]
                    new_lines: List[str] = []
                    for trade in trades:
                        tid = _trade_tid(trade)
                        if tid is None:
                            continue
                        if gate.add(tid):
                            new_lines.append(json.dumps(trade) + "\n")
                    if new_lines:
                        with open(
                            data_dir / f"trades_{coin}.jsonl", "a", encoding="utf-8"
                        ) as tf:
                            tf.writelines(new_lines)
                except Exception as trades_exc:
                    now_str = datetime.datetime.now().isoformat()
                    sys.stderr.write(
                        f"[{now_str}] trades poll error ({coin}): {trades_exc}\n"
                    )
                    sys.stderr.flush()

        except Exception as exc:
            now_str = datetime.datetime.now().isoformat()
            sys.stderr.write(f"[{now_str}] Polling error: {exc}\n")
            sys.stderr.flush()

        # Interruptible sleep preserving interval cadence
        elapsed = time.time() - loop_start
        sleep_remaining = max(0.0, interval - elapsed)
        target_wake = time.time() + sleep_remaining
        while running and time.time() < target_wake:
            time.sleep(min(0.2, max(0.0, target_wake - time.time())))

    sys.stderr.write("Sampler shut down cleanly.\n")
    sys.stderr.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("Sampler stopped by user.\n")
        sys.stderr.flush()
        sys.exit(0)
