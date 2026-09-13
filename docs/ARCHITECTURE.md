# Architecture

Four files matter; everything else is output.

```
Hyperliquid official client (subprocess, --json)
        │                    │
   sampler.py           server.py (on demand)
   every 12s            L2/funding/candles live
        │                    │
  data/oi_<COIN>.jsonl     (OI history)
  data/trades_<COIN>.jsonl (recent prints, last-10 polls)
        │                    │
        └──────► index.html (polls /api/*, renders)
```

## sampler.py

Invokes the official info client as a subprocess on a fixed cadence (default
12s) and appends one JSON line per coin per poll to `data/oi_<COIN>.jsonl`.
After every OI write of the tick, it fetches `recentTrades` for those coins
concurrently and appends new rows (deduped on `tid`, without the `users`
address pair) to `data/trades_<COIN>.jsonl`. A failed or slow trades poll
never skips the OI write and cannot stretch the OI cadence past one trades
timeout. No network stack in-repo; the client is a local file pointed at by
`--client`. This file is the only writer in the project.

## server.py

A stdlib `ThreadingHTTPServer` bound to `127.0.0.1` only. It serves
`index.html` and a read-only JSON API: `/api/oi/<COIN>` reads the tail of the
sampler's JSONL backwards until the requested lookback is covered;
`/api/prints/<COIN>` does the same for the trades file; `/api/l2`,
`/api/funding`, and `/api/profile` call the client on demand with a short TTL
cache so several open tabs cost one subprocess call per interval. `levels.yaml`
is re-parsed per request (prints reuse that reader to tag proximity). The
server never opens a file for writing — the CI greps that, and there is no
write path to the levels file for anything to reach.

## index.html

One self-contained page: no CDN, no build step, no external asset. It polls
the API and renders the OI×price quadrant, L2 depth, funding/premium, the
hand-edited levels strip, and the volume-at-price profile with notable
prints. Every colour comes from one CSS token set so the system dark mode
cannot regress.

## levels.yaml

Pure data, hand-edited by Wilson. The reader is a small stdlib parser for
exactly one shape; anything it cannot read is reported by line number rather
than silently dropped.

## The doctrine as architecture

Read-only is not a policy statement, it is the shape of the system. There is
no code path that places an order, holds a credential, or binds a non-
loopback socket — and the CI workflow greps all of that on every PR, so a
violation cannot merge.

## Honest coverage

The panels show what actually happened instead of a prettier picture:

- **Capped flag** — when a file is truncated or only partially covers the
  lookback, the panel is marked so a short window is never mistaken for a
  full one. Prints add `window_capped` when the trades-file line cap is
  what cut the window short, and OR that into `truncated`.
- **Veil** — on first paint and on empty data, the panel is covered with a
  "waiting" state rather than blank space or invented values.
- **Data-age badges** — age turns amber past 60s and `STALE` past 300s, so a
  paused sampler is visible the moment it should be.

## Ports and bind

`server.py` binds `127.0.0.1` only (the CI enforces this). Default port 8791;
8765 belongs to another dashboard on this machine. Both processes are local
first: the sampler writes, the board reads and serves to a browser on the
same machine.
