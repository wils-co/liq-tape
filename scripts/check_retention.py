#!/usr/bin/env python3
"""Offline checks for retention: old rows move to per-day gzip archives and
nothing is lost — not a row appended mid-rotation, not a row without a
time, not a chunk left behind by a crash."""

import gzip
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from retention import append_lines, cap_log, rotate_all, rotate_file  # noqa: E402

DAY = 86400


def utc_day(seconds: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(seconds))


def archived_rows(root: Path, name: str) -> list:
    rows = []
    for gz in sorted(root.glob(f"*/{name}.gz")):
        with gzip.open(gz, "rt") as f:
            rows.extend(json.loads(line) for line in f if line.startswith("{"))
    return rows


def main() -> int:
    now = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        archive = data / "archive"

        # 14 days of OI, one row per 6h, plus a row with no readable time
        # right after an old row: it must travel with that row.
        oi = data / "oi_BTC.jsonl"
        rows = [{"ts": now - d * DAY - h * 6 * 3600, "mark": 1.0} for d in range(13, -1, -1) for h in (3, 2, 1, 0)]
        lines = [json.dumps(r) + "\n" for r in rows]
        lines.insert(4, "not json\n")
        oi.write_text("".join(lines))

        trades = data / "trades_BTC.jsonl"
        trades.write_text("".join(json.dumps({"time": int((now - d * DAY) * 1000), "tid": d}) + "\n" for d in (12, 11, 2, 0)))

        liq = data / "liq_BTC.jsonl"
        liq.write_text("".join(json.dumps({"asof_ms": int((now - d * DAY) * 1000)}) + "\n" for d in (15, 1)))

        total_oi = len(lines)
        summary = rotate_all(data, 10, now)

        cutoff = now - 10 * DAY
        live = [json.loads(l) for l in oi.read_text().splitlines()]
        assert all(r["ts"] >= cutoff for r in live), "old row left in live file"
        arch = archived_rows(archive, "oi_BTC.jsonl")
        assert all(r["ts"] < cutoff for r in arch), "new row archived"
        # 14 days × 4 rows + the untimed row, split with nothing lost.
        untimed_archived = sum(1 for gz in archive.glob("*/oi_BTC.jsonl.gz") for l in gzip.open(gz, "rt") if l == "not json\n")
        assert untimed_archived == 1, "untimed row did not follow its neighbour"
        assert len(live) + len(arch) + untimed_archived == total_oi, (len(live), len(arch), total_oi)
        # Each archived row sits in the folder for its own UTC day.
        for gz in archive.glob("*/oi_BTC.jsonl.gz"):
            with gzip.open(gz, "rt") as f:
                for line in f:
                    if line.startswith("{"):
                        assert utc_day(json.loads(line)["ts"]) == gz.parent.name, gz

        assert [json.loads(l)["tid"] for l in trades.read_text().splitlines()] == [2, 0]
        assert sorted(r["tid"] for r in archived_rows(archive, "trades_BTC.jsonl")) == [11, 12]
        assert len(liq.read_text().splitlines()) == 1 and len(archived_rows(archive, "liq_BTC.jsonl")) == 1
        assert summary["files"] == 3, summary

        # A second pass the same day has nothing to do and rewrites nothing.
        mtime = oi.stat().st_mtime_ns
        assert rotate_all(data, 10, now)["archived"] == 0
        assert oi.stat().st_mtime_ns == mtime, "live file rewritten with nothing to archive"

        # A row appended between the scan and the swap is kept.
        oi.write_text(json.dumps({"ts": now - 20 * DAY}) + "\n" + json.dumps({"ts": now}) + "\n")
        late = json.dumps({"ts": now + 1, "late": True}) + "\n"
        rotate_file(oi, cutoff, archive, after_scan=lambda: append_lines(oi, [late]))
        tail = oi.read_text().splitlines()
        assert tail[-1] == late.strip() and len(tail) == 2, tail

        # Crash leftovers: a .part (crash before commit) is dropped because the
        # live file still has those rows; a .ready (after commit) is merged.
        day_dir = archive / utc_day(now - 30 * DAY)
        day_dir.mkdir(parents=True, exist_ok=True)
        with gzip.open(day_dir / "oi_ETH.jsonl.gz.part", "wt") as f:
            f.write('{"ts": 1}\n')
        with gzip.open(day_dir / "oi_ETH.jsonl.gz.ready", "wt") as f:
            f.write('{"ts": 2}\n')
        rotate_all(data, 10, now)
        assert not list(archive.glob("*/*.part")) and not list(archive.glob("*/*.ready"))
        assert [r["ts"] for r in archived_rows(archive, "oi_ETH.jsonl")] == [2]

        # Logs: capped by size once they double past the keep size, cut on a line.
        log = data / "board.log"
        log.write_bytes(b"".join(b"line %06d\n" % i for i in range(4000)))
        assert cap_log(log, keep_bytes=10_000)
        kept = log.read_bytes()
        assert len(kept) <= 10_000 and kept.startswith(b"line ") and kept.endswith(b"line 003999\n")

    print("retention checks: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
