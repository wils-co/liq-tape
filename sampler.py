#!/usr/bin/env python3
"""liq-tape OI, mark, and recent-trades sampler.

Polls the official Hyperliquid client helper for perpetual market contexts
and the last handful of trades, appending to per-coin jsonl files.
Read-only: no order execution, no credentials, no signal generation.
"""

import argparse
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

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
# Fields the panel actually reads. `users` is ~40% of each row and nothing
# consumes it; existing files keep old rows, this is append-forward.
TRADE_FIELDS: Tuple[str, ...] = ("coin", "side", "px", "sz", "time", "hash", "tid")
# liqmap: 200 leaderboard accounts × clearinghouseState (weight 2) on its own
# slower clock. Runs in the background; the kill timeout is its own, not the
# OI tick's.
DEFAULT_LIQ_INTERVAL: float = 120.0
LIQ_TIMEOUT: float = 90.0
LIQ_TOP: int = 200
LIQ_TAIL_BYTES: int = 64 * 1024
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


def slim_trade(trade: Dict[str, Any]) -> Dict[str, Any]:
    """Keep the fields the board reads. Drop ``users`` and anything else extra."""
    return {key: trade[key] for key in TRADE_FIELDS if key in trade}


def fetch_trades_for_coins(
    client_path: Path, coins: List[str]
) -> Dict[str, Tuple[Optional[List[Dict[str, Any]]], Optional[Exception]]]:
    """Fetch recent trades for each coin concurrently.

    Wall-clock wait is one timeout, not N sequential timeouts, so a slow
    or hung trades endpoint cannot stretch the OI cadence past one
    ``TRADES_TIMEOUT``. A failed coin is ``(None, exc)`` and does not
    prevent the others from returning.
    """
    out: Dict[str, Tuple[Optional[List[Dict[str, Any]]], Optional[Exception]]] = {
        coin: (None, None) for coin in coins
    }
    if not coins:
        return out
    workers = min(len(coins), 8)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(fetch_recent_trades, client_path, coin): coin for coin in coins
        }
        for fut in as_completed(futs):
            coin = futs[fut]
            try:
                out[coin] = (fut.result(), None)
            except Exception as exc:
                out[coin] = (None, exc)
    return out


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


def last_liq_asof(path: Path) -> Optional[int]:
    """`asof_ms` of the last line in a liq file, so a restart does not re-append it."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - LIQ_TAIL_BYTES))
            tail = f.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and isinstance(rec.get("asof_ms"), int):
            return rec["asof_ms"]
    return None


class LiqPoller:
    """Runs `liqmap` in the background on its own slower clock.

    The OI tick only ever calls `tick()`, which launches, checks, or kills the
    child without waiting on it — a hung liqmap cannot delay an OI write.
    Output goes to an unnamed temp file, not a pipe, so a large payload can
    never block the child on a full pipe buffer while nobody is reading.
    """

    def __init__(
        self, client_path: Path, coins: List[str], data_dir: Path, every: float, timeout: float
    ) -> None:
        self.client_path = client_path
        self.coins = coins
        self.data_dir = data_dir
        self.every = every
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None
        self.out: Any = None
        self.err: Any = None
        self.started = 0.0
        self.last_launch = 0.0
        self.last_asof: Dict[str, Optional[int]] = {
            coin: last_liq_asof(data_dir / f"liq_{coin}.jsonl") for coin in coins
        }

    def tick(self, now: float) -> None:
        if self.proc is None:
            if now - self.last_launch >= self.every:
                self._launch(now)
            return
        if self.proc.poll() is None:
            if now - self.started > self.timeout:
                self.proc.kill()
                self.proc.wait()
                _log(f"liqmap killed after {self.timeout:.0f}s")
                self._close()
            return
        try:
            if self.proc.returncode != 0:
                self.err.seek(0)
                raise RuntimeError(
                    f"liqmap exited with code {self.proc.returncode}: "
                    f"{self.err.read().decode('utf-8', errors='replace').strip()}"
                )
            self.out.seek(0)
            self.write(json.loads(self.out.read().decode("utf-8")))
        except Exception as exc:
            _log(f"liqmap poll error: {exc}")
        finally:
            self._close()

    def write(self, payload: Dict[str, Any]) -> None:
        asof = payload.get("asof_ms")
        if not isinstance(asof, int):
            raise ValueError("liqmap payload has no asof_ms")
        by_coin = payload.get("by_coin") or {}
        for coin in self.coins:
            if self.last_asof.get(coin) == asof:
                continue
            rows = by_coin.get(coin)
            record = {
                "asof_ms": asof,
                "coin": coin,
                "requested": payload.get("requested"),
                "fetched": payload.get("fetched"),
                "capped": bool(payload.get("capped")),
                "positions": rows if isinstance(rows, list) else [],
            }
            with open(self.data_dir / f"liq_{coin}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
            self.last_asof[coin] = asof

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self._close()

    def _launch(self, now: float) -> None:
        self.last_launch = now
        self.out = tempfile.TemporaryFile()
        self.err = tempfile.TemporaryFile()
        cmd = [
            sys.executable,
            str(self.client_path),
            "liqmap",
            "--coins",
            ",".join(self.coins),
            "--top",
            str(LIQ_TOP),
            "--json",
        ]
        try:
            self.proc = subprocess.Popen(cmd, stdout=self.out, stderr=self.err)
            self.started = now
        except OSError as exc:
            _log(f"liqmap launch error: {exc}")
            self._close()

    def _close(self) -> None:
        for handle in (self.out, self.err):
            if handle is not None:
                handle.close()
        self.proc = None
        self.out = None
        self.err = None


def _log(message: str) -> None:
    sys.stderr.write(f"[{datetime.datetime.now().isoformat()}] {message}\n")
    sys.stderr.flush()


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
    parser.add_argument(
        "--no-liq",
        action="store_true",
        help="Do not poll liquidation prices (liqmap)",
    )
    parser.add_argument(
        "--liq-interval",
        type=float,
        default=DEFAULT_LIQ_INTERVAL,
        help=f"Seconds between liqmap polls (default: {DEFAULT_LIQ_INTERVAL:.0f})",
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

    liq: Optional[LiqPoller] = None
    if not args.no_liq:
        liq = LiqPoller(
            client_path, coins, data_dir, max(30.0, args.liq_interval), LIQ_TIMEOUT
        )

    # Required single startup line
    print(
        f"liq-tape sampler started | coins: {', '.join(coins)} | interval: {interval}s | data: {data_dir}"
        f" | liq: {'off' if liq is None else f'{liq.every:.0f}s'}",
        flush=True,
    )

    while running:
        loop_start = time.time()
        if liq is not None:
            try:
                liq.tick(loop_start)
            except Exception as exc:
                _log(f"liqmap tick error: {exc}")
        try:
            payload = fetch_markets(client_path)
            raw_markets = payload.get("markets", [])
            lookup: Dict[str, Dict[str, Any]] = {
                m.get("coin"): m for m in raw_markets if isinstance(m, dict)
            }
            sample_ts = round(time.time(), 3)

            written: List[str] = []
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
                written.append(coin)

            # Trades run after every OI write, concurrently, so four hung
            # endpoints cost one timeout rather than four sequential ones.
            fetched = fetch_trades_for_coins(client_path, written)
            for coin in written:
                trades, trades_exc = fetched[coin]
                if trades_exc is not None:
                    now_str = datetime.datetime.now().isoformat()
                    sys.stderr.write(
                        f"[{now_str}] trades poll error ({coin}): {trades_exc}\n"
                    )
                    sys.stderr.flush()
                    continue
                gate = tid_gates[coin]
                new_lines: List[str] = []
                for trade in trades or []:
                    tid = _trade_tid(trade)
                    if tid is None:
                        continue
                    if gate.add(tid):
                        new_lines.append(json.dumps(slim_trade(trade)) + "\n")
                if new_lines:
                    with open(
                        data_dir / f"trades_{coin}.jsonl", "a", encoding="utf-8"
                    ) as tf:
                        tf.writelines(new_lines)

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

    if liq is not None:
        liq.stop()
    sys.stderr.write("Sampler shut down cleanly.\n")
    sys.stderr.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("Sampler stopped by user.\n")
        sys.stderr.flush()
        sys.exit(0)
