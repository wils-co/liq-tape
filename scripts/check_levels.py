#!/usr/bin/env python3
"""Fail if levels.yaml does not read cleanly, or if the reader went lenient.

Deliberately not a second implementation of the shape rules: it imports the
reader the dashboard actually serves from, so CI can never pass a file the
server would report as broken and the two can never drift apart.

The re-validation loop after the parse is not belt-and-braces on the file —
``parse_levels`` already refuses a bad entry. It is a regression guard on the
parser: if that function is ever relaxed into emitting an entry with a
non-numeric price or an unknown kind, this fails even though the file itself
never changed.

Exit 0 and print a count on success; exit 1 naming every unreadable line
otherwise. No arguments, no dependencies, no network, writes nothing.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import server  # noqa: E402  - resolved against REPO_ROOT above


def main() -> int:
    path = Path(server.LEVELS_FILE)
    if not path.is_file():
        print(f"check_levels: {path} is missing")
        return 1

    coins, errors = server.parse_levels(path.read_text(encoding="utf-8"))

    for err in errors:
        print(f"{path.name}:{err['line']}: {err['detail']}")
    if errors:
        print(f"check_levels: {len(errors)} unreadable line(s)")
        return 1

    bad = 0
    total = 0
    for coin, entries in sorted(coins.items()):
        for lvl in entries:
            total += 1
            if not isinstance(lvl["price"], float):
                print(f"{path.name}:{lvl['line']}: {coin} price is not a number")
                bad += 1
            if not isinstance(lvl["label"], str):
                print(f"{path.name}:{lvl['line']}: {coin} label is not text")
                bad += 1
            if lvl["kind"] not in server.LEVEL_KINDS:
                print(f"{path.name}:{lvl['line']}: {coin} kind {lvl['kind']!r} is not allowed")
                bad += 1
    if bad:
        print(f"check_levels: {bad} entr(ies) the reader should have refused")
        return 1

    if not total:
        print(f"check_levels: {path.name} parsed but holds no levels")
        return 1

    print(f"check_levels: ok — {total} level(s) across {len(coins)} coin(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
