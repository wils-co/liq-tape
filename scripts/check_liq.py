#!/usr/bin/env python3
"""Offline checks for the liq map: two account sets merge into one line per
coin, an account in both sets counts once, and a PR9 line still serves.

No network, no client: payloads are built here and fed to the sampler's
LiqPoller and the server's builder directly.
"""

import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sampler import LiqPoller  # noqa: E402
from server import build_liq, cluster_liq  # noqa: E402


def row(address: str, szi: float, px: float, value: float) -> dict:
    return {"address": address, "coin": "BTC", "szi": szi, "liquidation_px": px, "position_value": value}


def payload(asof_ms: int, rows: list) -> dict:
    return {"asof_ms": asof_ms, "requested": 3, "fetched": 3, "capped": False, "by_coin": {"BTC": rows}}


def main() -> int:
    now_ms = int(time.time() * 1000)
    with tempfile.TemporaryDirectory() as tmp:
        data = Path(tmp)
        (data / "oi_BTC.jsonl").write_text(json.dumps({"ts": time.time(), "mark": 100000.0}) + "\n")

        sets = {"active": {"top": 3, "every": 120.0}, "largest": {"top": 3, "every": 600.0}}
        poller = LiqPoller(Path("/nonexistent/client.py"), ["BTC"], data, sets, 90.0)

        # 0xB is in both sets: the active poll is newer, so its row wins.
        poller.absorb("largest", payload(now_ms - 300_000, [row("0xA", -2, 130000.0, 5e7), row("0xB", 1, 90000.0, 1e6)]))
        poller.absorb("active", payload(now_ms - 20_000, [row("0xB", 1, 98500.0, 2e6), row("0xC", -1, 101000.0, 3e6)]))
        poller.write()

        line = json.loads((data / "liq_BTC.jsonl").read_text().splitlines()[-1])
        assert set(line["sets"]) == {"active", "largest"}, line["sets"]
        assert line["asof_ms"] == now_ms - 20_000, line["asof_ms"]
        addrs = sorted((r["address"], r["set"]) for r in line["positions"])
        assert addrs == [("0xA", "largest"), ("0xB", "active"), ("0xC", "active")], addrs
        assert all("coin" not in r for r in line["positions"]), "per-coin rows keep coin"
        assert line["sets"]["largest"]["interval_s"] == 600.0

        served = build_liq(data, "BTC", time.time())
        cov = served["coverage"]["sets"]
        assert 290 <= cov["largest"]["age_s"] <= 320 and cov["active"]["age_s"] < 60, cov
        assert served["within_2pct"]["positions"] == 2, served["within_2pct"]  # 0xB @98.5k, 0xC @101k

        # A restart resumes both sets from the file and does not re-poll a fresh one.
        again = LiqPoller(Path("/nonexistent/client.py"), ["BTC"], data, sets, 90.0)
        assert set(again.snaps) == {"active", "largest"}, again.snaps.keys()
        assert time.time() - again.last_launch["largest"] < 600, again.last_launch

        # A PR9 line (no sets, rows with coin) still serves, as the largest set.
        legacy = {"asof_ms": now_ms, "coin": "BTC", "requested": 200, "fetched": 200, "capped": False,
                  "positions": [row("0xD", -1, 101500.0, 1e6)]}
        (data / "liq_BTC.jsonl").write_text(json.dumps(legacy) + "\n")
        served = build_liq(data, "BTC", time.time())
        assert list(served["coverage"]["sets"]) == ["largest"], served["coverage"]
        assert len(served["clusters"]) == 1, served["clusters"]

    # The same address twice in one list clusters once.
    dup = [row("0xE", -1, 100000.0, 1e6), row("0xE", -1, 100000.0, 1e6)]
    assert cluster_liq(dup, "BTC")[0]["positions"] == 1

    print("liq checks: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
