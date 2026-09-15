"""Daily retention for the sampler's data files. Archive, never delete.

Hyperliquid has no OI-history endpoint, so `data/` is the only copy of what
the sampler recorded. Retention keeps the live files short without losing
any of it: rows older than the retention window move into gzipped per-day
archives, and the live file is rewritten with the rest.

    data/oi_BTC.jsonl                         live, last N days
    data/archive/2026-09-12/oi_BTC.jsonl.gz   rows from that UTC day

Only the sampler imports this; it stays the only writer in the project.
Every append goes through `append_lines`, which holds a per-file lock, so a
rotation running in a background thread cannot lose a row appended while it
works: it scans a size snapshot without the lock, then takes the lock only
to copy whatever arrived since and swap the file in.

Crash safety: archive rows are written to `.part` files, renamed `.ready`
before the live file is replaced, and merged into the day's `.gz` after.
A crash before the rename leaves `.part` files (deleted next run; the live
file still has the rows). A crash between the rename and the replace can
archive a day's rows twice — duplicates, never loss.
"""

import datetime
import gzip
import json
import os
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

DEFAULT_RETAIN_DAYS: int = 10
# Process logs are request noise, not history: capped by size, not archived.
LOG_KEEP_BYTES: int = 5 * 1024 * 1024
ARCHIVE_DIRNAME: str = "archive"

# File prefix -> (time field, divisor to seconds). A row's UTC day is its
# archive folder.
TIME_FIELDS: Dict[str, Tuple[str, float]] = {
    "oi": ("ts", 1.0),
    "trades": ("time", 1000.0),
    "liq": ("asof_ms", 1000.0),
}


class _FileLocks:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: Dict[str, threading.Lock] = {}

    def get(self, path: Path) -> threading.Lock:
        key = str(Path(path).resolve())
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = self._locks[key] = threading.Lock()
            return lock


LOCKS = _FileLocks()


def append_lines(path: Path, lines: Any) -> None:
    """Append text lines (each ending in a newline) under the file's lock."""
    with LOCKS.get(path):
        with open(path, "a", encoding="utf-8") as f:
            f.writelines(lines)


def _row_time(raw: bytes, field: str, divisor: float) -> Optional[float]:
    try:
        rec = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(rec, dict):
        return None
    value = rec.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) / divisor


def _utc_day(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc).strftime("%Y-%m-%d")


def finalize_ready(archive_root: Path) -> int:
    """Merge committed `.ready` chunks into their day's `.gz` (concatenated
    gzip members are one valid gzip stream)."""
    merged = 0
    if not archive_root.is_dir():
        return merged
    for ready in sorted(archive_root.glob("*/*.gz.ready")):
        target = ready.with_name(ready.name[: -len(".ready")])
        with open(ready, "rb") as src, open(target, "ab") as dst:
            dst.write(src.read())
            dst.flush()
            os.fsync(dst.fileno())
        ready.unlink()
        merged += 1
    return merged


def rotate_file(
    path: Path,
    cutoff_s: float,
    archive_root: Path,
    after_scan: Optional[Callable[[], None]] = None,
) -> Tuple[int, int]:
    """Move rows older than ``cutoff_s`` into per-day archives.

    Returns (archived, kept). A file with nothing old is not rewritten.
    ``after_scan`` is a test hook, called between the unlocked scan and the
    locked swap.
    """
    kind = path.name.split("_", 1)[0]
    field, divisor = TIME_FIELDS[kind]

    with LOCKS.get(path):
        size = path.stat().st_size

    tmp = path.with_name(path.name + ".rotating")
    parts: Dict[str, Any] = {}
    archived = kept = 0
    prev_old = False
    prev_day: Optional[str] = None
    try:
        with open(path, "rb") as src, open(tmp, "wb") as keep:
            pos = 0
            while pos < size:
                raw = src.readline()
                if not raw:
                    break
                pos += len(raw)
                t = _row_time(raw, field, divisor)
                if t is not None:
                    prev_old = t < cutoff_s
                    prev_day = _utc_day(t)
                # A row without a readable time follows the row before it.
                if prev_old and prev_day is not None:
                    part = parts.get(prev_day)
                    if part is None:
                        day_dir = archive_root / prev_day
                        day_dir.mkdir(parents=True, exist_ok=True)
                        part = parts[prev_day] = gzip.open(day_dir / f"{path.name}.gz.part", "wb")
                    part.write(raw if raw.endswith(b"\n") else raw + b"\n")
                    archived += 1
                else:
                    keep.write(raw)
                    kept += 1
        for part in parts.values():
            part.close()
        parts = {}

        if archived == 0:
            tmp.unlink()
            return 0, kept

        # Commit point for the archive side: from here a crash can duplicate
        # rows in the archive but cannot lose them.
        for part_path in archive_root.glob(f"*/{path.name}.gz.part"):
            os.replace(part_path, part_path.with_name(part_path.name[: -len(".part")] + ".ready"))

        if after_scan is not None:
            after_scan()

        with LOCKS.get(path):
            with open(path, "rb") as src, open(tmp, "ab") as keep:
                src.seek(size)
                tail = src.read()
                keep.write(tail)
                keep.flush()
                os.fsync(keep.fileno())
            kept += tail.count(b"\n")
            os.replace(tmp, path)
    finally:
        for part in parts.values():
            part.close()
        if tmp.exists():
            tmp.unlink()

    finalize_ready(archive_root)
    return archived, kept


def cap_log(path: Path, keep_bytes: int = LOG_KEEP_BYTES) -> bool:
    """Keep the last ``keep_bytes`` of a log once it doubles past that.

    The run scripts hold these open with O_APPEND, so writes land at the new
    end after the truncate; a line written during the rewrite can be lost,
    which is acceptable for a log and not for data.
    """
    if path.stat().st_size <= keep_bytes * 2:
        return False
    with open(path, "r+b") as f:
        f.seek(-keep_bytes, os.SEEK_END)
        tail = f.read()
        cut = tail.find(b"\n")
        if cut >= 0:
            tail = tail[cut + 1 :]
        f.seek(0)
        f.write(tail)
        f.truncate()
    return True


def rotate_all(data_dir: Path, retain_days: int, now: float) -> Dict[str, Any]:
    """One retention pass over every data file and log in ``data_dir``."""
    archive_root = data_dir / ARCHIVE_DIRNAME
    for stale in archive_root.glob("*/*.gz.part") if archive_root.is_dir() else []:
        stale.unlink()
    finalize_ready(archive_root)

    cutoff = now - retain_days * 86400
    summary: Dict[str, Any] = {"archived": 0, "files": 0, "logs_capped": 0}
    for kind in TIME_FIELDS:
        for path in sorted(data_dir.glob(f"{kind}_*.jsonl")):
            archived, _kept = rotate_file(path, cutoff, archive_root)
            if archived:
                summary["archived"] += archived
                summary["files"] += 1
    for log in sorted(data_dir.glob("*.log")):
        if cap_log(log):
            summary["logs_capped"] += 1
    return summary
