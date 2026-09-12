#!/usr/bin/env python3
"""liq-tape OI and mark sampler.

Polls the official Hyperliquid client helper for perpetual market contexts
and appends timestamped records to per-coin jsonl files.
Read-only: no order execution, no credentials, no signal generation.
"""

import argparse
import datetime
import json
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

DEFAULT_INTERVAL: float = 12.0
DEFAULT_COINS: List[str] = ["BTC", "ETH", "HYPE", "SOL"]
BASE_DIR: Path = Path(__file__).resolve().parent
DEFAULT_DATA_DIR: Path = BASE_DIR / "data"
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
