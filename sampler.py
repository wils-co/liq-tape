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
import threading
import time
from typing import Any, Deque, Dict, List, Optional, Set, Tuple

from retention import DEFAULT_RETAIN_DAYS, append_lines, rotate_all

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
# Two account sets, measured 2026-09-15: of 100 accounts, the top by account
# value held 0 liq prices within 5% of mark; the top by weekly volume /
# equity held 11. Budget: sampler ~520/min + board ~100/min of the 1200/min
# REST weight; 200×2 per 600s + 300×2 per 120s adds ~340/min.
DEFAULT_LIQ_INTERVAL: float = 120.0
DEFAULT_LIQ_LARGEST_INTERVAL: float = 600.0
LIQ_ACTIVE_TOP: int = 300
LIQ_LARGEST_TOP: int = 200
LIQ_TIMEOUT: float = 90.0
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


def last_liq_record(path: Path) -> Optional[Dict[str, Any]]:
    """Newest line of a liq file, so a restart resumes each set's snapshot
    instead of blanking the slow set until its next poll."""
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
            return rec
    return None


def slim_liq_row(row: Dict[str, Any], liq_set: str) -> Optional[Dict[str, Any]]:
    """The fields /api/liq reads, rounded. The file is per coin, so `coin`
    is dropped; `set` says which account ranking the row came from."""
    try:
        return {
            "address": str(row["address"]),
            "szi": round(float(row["szi"]), 6),
            "liquidation_px": float(f"{float(row['liquidation_px']):.7g}"),
            "position_value": round(float(row["position_value"]), 0),
            "set": liq_set,
        }
    except (KeyError, TypeError, ValueError):
        return None


class LiqPoller:
    """Runs `liqmap` in the background, one account set at a time.

    Two sets on their own clocks: `largest` (by account value; far liq
    prices, slow) and `active` (weekly volume / equity; near liq prices,
    fast). At most one child runs, so the sets never burst the shared
    1200/min REST weight together. The OI tick only ever calls `tick()`,
    which launches, checks, or kills the child without waiting on it — a
    hung liqmap cannot delay an OI write. Output goes to an unnamed temp
    file, not a pipe, so a large payload cannot block the child.

    Each finished poll appends one merged line per coin carrying every set's
    latest snapshot with its own age and coverage. An account in both sets
    keeps the row from the newer poll.
    """

    def __init__(
        self,
        client_path: Path,
        coins: List[str],
        data_dir: Path,
        sets: Dict[str, Dict[str, float]],
        timeout: float,
    ) -> None:
        self.client_path = client_path
        self.coins = coins
        self.data_dir = data_dir
        self.sets = sets
        self.timeout = timeout
        self.proc: Optional[subprocess.Popen] = None
        self.running_set: Optional[str] = None
        self.out: Any = None
        self.err: Any = None
        self.started = 0.0
        self.last_launch: Dict[str, float] = {name: 0.0 for name in sets}
        # set -> {asof_ms, requested, fetched, capped, by_coin: {coin: [rows]}}
        self.snaps: Dict[str, Dict[str, Any]] = {}
        self._seed()

    def _seed(self) -> None:
        for coin in self.coins:
            rec = last_liq_record(self.data_dir / f"liq_{coin}.jsonl")
            if rec is None:
                continue
            metas = rec.get("sets")
            if not isinstance(metas, dict):
                # A PR9 line: one set, the largest accounts.
                metas = {
                    "largest": {k: rec.get(k) for k in ("asof_ms", "requested", "fetched", "capped")}
                }
            for name, meta in metas.items():
                if name not in self.sets or not isinstance(meta, dict):
                    continue
                if not isinstance(meta.get("asof_ms"), int):
                    continue
                snap = self.snaps.get(name)
                if snap is None:
                    snap = {k: meta.get(k) for k in ("asof_ms", "requested", "fetched", "capped")}
                    snap["by_coin"] = {}
                    self.snaps[name] = snap
                rows = [
                    r for r in rec.get("positions") or []
                    if isinstance(r, dict) and r.get("set", "largest") == name
                ]
                snap["by_coin"][coin] = [r for r in (slim_liq_row(row, name) for row in rows) if r]
                # A restart does not re-poll a set that is still fresh.
                self.last_launch[name] = max(self.last_launch[name], meta["asof_ms"] / 1000.0)

    def tick(self, now: float) -> None:
        if self.proc is None:
            due = [
                name for name, cfg in self.sets.items()
                if now - self.last_launch[name] >= cfg["every"]
            ]
            if due:
                self._launch(min(due, key=lambda name: self.last_launch[name]), now)
            return
        if self.proc.poll() is None:
            if now - self.started > self.timeout:
                self.proc.kill()
                self.proc.wait()
                _log(f"liqmap ({self.running_set}) killed after {self.timeout:.0f}s")
                self._close()
            return
        try:
            if self.proc.returncode != 0:
                self.err.seek(0)
                raise RuntimeError(
                    f"liqmap ({self.running_set}) exited with code {self.proc.returncode}: "
                    f"{self.err.read().decode('utf-8', errors='replace').strip()}"
                )
            self.out.seek(0)
            self.absorb(self.running_set or "", json.loads(self.out.read().decode("utf-8")))
            self.write()
        except Exception as exc:
            _log(f"liqmap ({self.running_set}) poll error: {exc}")
        finally:
            self._close()

    def absorb(self, name: str, payload: Dict[str, Any]) -> None:
        asof = payload.get("asof_ms")
        if name not in self.sets or not isinstance(asof, int):
            raise ValueError("liqmap payload has no asof_ms or an unknown set")
        by_coin_raw = payload.get("by_coin") or {}
        by_coin: Dict[str, List[Dict[str, Any]]] = {}
        for coin in self.coins:
            rows = by_coin_raw.get(coin) if isinstance(by_coin_raw.get(coin), list) else []
            by_coin[coin] = [r for r in (slim_liq_row(row, name) for row in rows) if r]
        self.snaps[name] = {
            "asof_ms": asof,
            "requested": payload.get("requested"),
            "fetched": payload.get("fetched"),
            "capped": bool(payload.get("capped")),
            "by_coin": by_coin,
        }

    def write(self) -> None:
        if not self.snaps:
            return
        newest_first = sorted(self.snaps.items(), key=lambda kv: -kv[1]["asof_ms"])
        for coin in self.coins:
            seen: Set[str] = set()
            positions: List[Dict[str, Any]] = []
            for _name, snap in newest_first:
                for row in snap["by_coin"].get(coin) or []:
                    if row["address"] in seen:
                        continue
                    seen.add(row["address"])
                    positions.append(row)
            record = {
                "asof_ms": newest_first[0][1]["asof_ms"],
                "coin": coin,
                "requested": sum(int(s["requested"] or 0) for s in self.snaps.values()),
                "fetched": sum(int(s["fetched"] or 0) for s in self.snaps.values()),
                "capped": any(bool(s["capped"]) for s in self.snaps.values()),
                "sets": {
                    name: {
                        "asof_ms": snap["asof_ms"],
                        "requested": snap["requested"],
                        "fetched": snap["fetched"],
                        "capped": snap["capped"],
                        "interval_s": self.sets[name]["every"],
                    }
                    for name, snap in self.snaps.items()
                },
                "positions": positions,
            }
            append_lines(
                self.data_dir / f"liq_{coin}.jsonl",
                [json.dumps(record, separators=(",", ":")) + "\n"],
            )

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self._close()

    def _launch(self, name: str, now: float) -> None:
        self.last_launch[name] = now
        self.running_set = name
        self.out = tempfile.TemporaryFile()
        self.err = tempfile.TemporaryFile()
        cmd = [
            sys.executable,
            str(self.client_path),
            "liqmap",
            "--coins",
            ",".join(self.coins),
            "--set",
            name,
            "--top",
            str(int(self.sets[name]["top"])),
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
        self.running_set = None
        self.out = None
        self.err = None


class Retention:
    """Runs the daily archive pass (retention.py) in a background thread.

    First pass a couple of minutes after start, so a restart does not add a
    file rewrite to the moment the sampler is catching up; then once per UTC
    day, a few minutes after midnight. The OI tick only calls `tick()`, which
    starts the thread and returns. Appends and the pass share per-file locks.
    """

    FIRST_DELAY_S: float = 120.0
    AFTER_MIDNIGHT_S: float = 300.0

    def __init__(self, data_dir: Path, retain_days: int, now: float) -> None:
        self.data_dir = data_dir
        self.retain_days = retain_days
        self.next_due = now + self.FIRST_DELAY_S
        self.thread: Optional[threading.Thread] = None

    def tick(self, now: float) -> None:
        if now < self.next_due or (self.thread is not None and self.thread.is_alive()):
            return
        day = 86400.0
        self.next_due = (now // day + 1) * day + self.AFTER_MIDNIGHT_S
        self.thread = threading.Thread(target=self._run, args=(now,), name="retention")
        self.thread.start()

    def _run(self, now: float) -> None:
        started = time.time()
        try:
            summary = rotate_all(self.data_dir, self.retain_days, now)
            _log(
                f"retention: archived {summary['archived']} rows from {summary['files']} files"
                f" (older than {self.retain_days}d), capped {summary['logs_capped']} logs"
                f" in {time.time() - started:.1f}s"
            )
        except Exception as exc:
            _log(f"retention error: {exc}")

    def stop(self) -> None:
        if self.thread is not None:
            self.thread.join()


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
        help=f"Seconds between polls of the active account set (default: {DEFAULT_LIQ_INTERVAL:.0f})",
    )
    parser.add_argument(
        "--liq-largest-interval",
        type=float,
        default=DEFAULT_LIQ_LARGEST_INTERVAL,
        help=f"Seconds between polls of the largest account set (default: {DEFAULT_LIQ_LARGEST_INTERVAL:.0f})",
    )
    parser.add_argument(
        "--retain-days",
        type=int,
        default=DEFAULT_RETAIN_DAYS,
        help=f"Days of rows kept in the live data files; older rows move to data/archive/ (default: {DEFAULT_RETAIN_DAYS}, 0 = off)",
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
            client_path,
            coins,
            data_dir,
            {
                "active": {"top": LIQ_ACTIVE_TOP, "every": max(30.0, args.liq_interval)},
                "largest": {"top": LIQ_LARGEST_TOP, "every": max(30.0, args.liq_largest_interval)},
            },
            LIQ_TIMEOUT,
        )

    retention: Optional[Retention] = None
    if args.retain_days > 0:
        retention = Retention(data_dir, args.retain_days, time.time())

    liq_desc = "off" if liq is None else ", ".join(
        f"{name} {cfg['top']:.0f}@{cfg['every']:.0f}s" for name, cfg in liq.sets.items()
    )

    # Required single startup line
    print(
        f"liq-tape sampler started | coins: {', '.join(coins)} | interval: {interval}s | data: {data_dir}"
        f" | liq: {liq_desc}"
        f" | retain: {'off' if retention is None else f'{args.retain_days}d live, older archived'}",
        flush=True,
    )

    while running:
        loop_start = time.time()
        if retention is not None:
            retention.tick(loop_start)
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

                append_lines(data_dir / f"oi_{coin}.jsonl", [json.dumps(record) + "\n"])
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
                    append_lines(data_dir / f"trades_{coin}.jsonl", new_lines)

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
    if retention is not None:
        retention.stop()
    sys.stderr.write("Sampler shut down cleanly.\n")
    sys.stderr.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.stderr.write("Sampler stopped by user.\n")
        sys.stderr.flush()
        sys.exit(0)
