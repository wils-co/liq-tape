# liq-tape

Real-time, read-only liquidity and open interest instrument panel for discretionary trading.

## Doctrine
- **Read-only forever:** Surfaces market data, structure, and open interest regimes. Never generates trade cues, alerts, or signals.
- **No orders:** Zero order placement or execution capabilities. Wilson's discretion is the trading brain; this dashboard is the instrument panel.
- **No keys:** Zero credential or signing infrastructure. No private keys, secret files, or external trading permissions.
- **No signals:** Displays raw and regime-contextualized data without buy/sell bias.

## Scope (PR1: Foundation Sampler)
PR1 provides the local background sampler recording perpetual market contexts (mark price, open interest, funding rate, and premium) into append-only JSONL files.

- Data source: the Hyperliquid official info client (`hyperliquid_client.py markets --json`), invoked as a subprocess. Point `--client` at your local copy of the official client.
- Target files: `data/oi_BTC.jsonl`, `data/oi_ETH.jsonl`, `data/oi_HYPE.jsonl`, `data/oi_SOL.jsonl`
- Polling cadence: Configurable (default 12s)

## Quickstart

```bash
./run_sampler.sh start && ./run_board.sh start
```

Two processes, two commands. The sampler polls and writes `data/`; the board
serves `http://127.0.0.1:8791` from those files plus the client's live
endpoints. The quadrant shows an honest empty state until the lookback window
has filled — roughly 15 minutes of sampling at the default 12s cadence. It does
not invent numbers to fill the window.

Default coins: BTC, ETH, HYPE, SOL. Change the sampler's set with
`--coins BTC,ETH` — the board serves whatever coins exist in `data/`.

## Usage

### Sampler — run directly
```bash
python3 sampler.py
```

Options:
- `--interval <seconds>`: Polling interval in seconds (default: `12.0`)
- `--data-dir <path>`: Directory where `.jsonl` files are stored (default: `data/`)
- `--coins <COIN1,COIN2,...>`: Comma-separated list of coins (default: `BTC,ETH,HYPE,SOL`)
- `--client <path>`: Path to `hyperliquid_client.py`
- `--no-liq`: Skip the background liqmap poll (PR9)
- `--liq-interval <seconds>`: Seconds between liqmap polls (default: `120`)

### Run as background daemon
```bash
./run_sampler.sh start   # starts the sampler via nohup
./run_sampler.sh status  # checks running status
./run_sampler.sh stop    # stops the background process
./run_sampler.sh log     # follows log output
```

The board has the same interface: `./run_board.sh start|stop|status|restart|log`.
See Operations below.

## Scope (PR2: Server + OI x Price quadrant)
PR2 adds the local dashboard: a stdlib HTTP server over the sampler's JSONL output,
and a single-file page showing the OI-vs-price quadrant. No frameworks, no npm,
no external assets, no dependencies. Binds `127.0.0.1` only.

### Run
```bash
python3 server.py            # http://127.0.0.1:8791
```

Options:
- `--port <n>`: loopback port (default: `8791`)
- `--data-dir <path>`: directory holding `oi_<COIN>.jsonl` (default: `data/`)
- `--levels <path>`: the hand-edited levels file (default: `levels.yaml`)

Run `./run_sampler.sh start` first — the page reads the sampler's files and shows a
"waiting for data" state until a lookback window has filled.

### API
`GET /api/oi/<COIN>?lookback=15m|1h|4h` (default `1h`) returns the newest sample, the
first sample inside the lookback window, sample count, data age in seconds, window
coverage, and the mark/OI percentage deltas across the window.

- `404` for a coin with no sampler file; `400` for an unrecognised lookback
- JSON error shape (`error` + `hint`) for missing or empty data — never a fabricated number
- Reads the tail of each file backwards in chunks, stopping once the window is
  covered, so cost tracks the lookback rather than total file growth

### Panel
BTC | ETH | HYPE | SOL toggle, last price, and a data-age badge (amber past 60s, red and marked
`STALE` past 300s). The quadrant plots price delta on X against open-interest delta on
Y, polling every 5s. The four regimes — trend (OI up, price up), squeeze (OI down,
price up), fresh shorts (OI up, price down), long flush (OI down, price down) — are
rendered as lens text in the corners. They are reading aids, not instructions: the
dot's position is the whole of the information.

## Scope (PR4: structure levels + UI config)

### Your levels
`levels.yaml` in the repo root holds the levels the dashboard draws. It is
**human-edited**: nothing in this repo writes to it, generates entries, detects
levels, or ranks them. Panel ⑥ renders what is written and stops there — what a
level means is yours to decide.

```yaml
levels:
  BTC:
    - {price: 79774, label: "swept pool (Sep 9 highs)", kind: pool}
    - {price: 76054, label: "swept-reclaimed base (Sep 2 lows)", kind: base}
```

- `kind` is one of `pool` (liquidity target, dashed amber), `base` (a shelf the
  price has worked off, dashed grey), or `session` (intraday marker, dotted).
  Omitted, it reads as `base`.
- `label` is free text and may contain `#`, commas and colons when quoted.
- Edit the file, save, refresh the page. The server re-reads and re-parses on
  every request, so no restart is needed anywhere.
- A coin with no entry draws nothing — no empty panel, no placeholder.

The reader is a deliberately small stdlib parser for exactly the shape above
(no PyYAML, no dependency): `levels:`, one indented coin key per block, and
`- {price: .., label: .., kind: ..}` entries — or the same keys on indented
lines under a bare `-`. Anything it cannot read is reported by line number in
the panel header rather than silently dropped, so a typo is visible.

### API
`GET /api/levels` returns the whole file as JSON — `coins` keyed by ticker with
each level's price, label, kind and source line (highest price first), plus an
`errors` list naming any unreadable line. `404` with `error: no_levels_file`
when the file is absent.

### Panel ⑥ — structure levels
A price-anchored strip under the pair, with its own scale centred on the last
price: levels above the price sit above the middle line, levels below sit
below, each labelled at the right edge with its distance from the last price on
the left. It is deliberately *not* drawn across the quadrant — the quadrant's Y
axis is open-interest delta, so a price drawn on it would be a number on the
wrong axis.

### Theme and coins
The page follows the system light/dark setting (`prefers-color-scheme`); every
colour, including the ones inside the SVG panels, comes from one token set. No
manual toggle. The header toggle covers BTC, ETH, HYPE and SOL, matching the
sampler's defaults.

## Scope (PR6: volume profile + notable prints)

Executed flow, next to the book's resting size. Two additions, one panel.

### Volume profile
`GET /api/profile/<COIN>?lookback=4h|12h|24h` (default `24h`) fetches 15m
candles from the official client and builds a price histogram (~48 buckets
across the window's high–low) plus session VWAP
(`Σ((h+l+c)/3 × v) / Σv`). Each candle's volume is split evenly across the
buckets its high–low span covers — 15m bars have no intra-bar distribution,
and the panel says so. Cached 60s per (coin, lookback). px/ohlc/v arrive as
strings and are converted to floats before they leave the server.

### Notable prints
The sampler now also polls `recentTrades` (last ~10 prints, not a full tape)
on the same 12s cadence and appends new rows to `data/trades_<COIN>.jsonl`,
deduped on `tid`, without the `users` address pair. A restart resumes the
watermark from the file tail. Trades fetches for the tracked coins run
concurrently after the OI writes, so a hung trades endpoint cannot stretch
the OI cadence past one timeout.

`GET /api/prints/<COIN>?lookback=15m|1h|4h&min_notional=<usd>` (default `1h` /
`$25,000`) reads that file, aggregates fills that share a taker-order
`hash` and side (sum `sz`, size-weighted `px`) *before* the notional
filter, and tags prints within 0.15% of a `levels.yaml` entry. Cap 200 with an honest
`truncated` flag; `window_capped` is set when the file tail cap cut the
window short. A missing trades file is an empty list with a note, not a
404 — the sampler may be older than the board. Side is `B` or `A`
(bid-taker / ask-taker); the page does not relabel it. Panel ⑦ labels
the prints readout `prints · 1h` — that window is not the profile chip.

### Panel ⑦
Full-width histogram under the levels strip: volume-at-price bars, VWAP line,
print ticks on the price axis with a level's label when the print is near one.
Own 4h/12h/24h chips. Dark mode is the same CSS tokens as the rest of the
page. Gaps in the tape are expected and labelled.

## Scope (PR7: walls, sampled CVD, layer chips)

Data the board already had: the L2 book and the sampled trades file. No new
client command, no time×price canvas.

### Walls
Each L2 level now carries `notional` (`px × sz`) and `wall` (boolean). A wall
is a level whose notional is at or above **1.5× the median notional of that
side**, or **20% of that side's visible notional** — either is enough. No
dollar floor. `wall_threshold` reports both cutoffs per side (`median_1_5`,
`share_20`); a side with fewer than 3 levels has no walls and omits the
cutoffs. Width of a bar still maps size; walls are drawn heavier and
labelled with notional. Tagged on `GET /api/l2/<COIN>` so the existing 2s
cache is the only book fetch.

### CVD
`GET /api/cvd/<COIN>?lookback=15m|1h|4h` (default `1h`) walks the trades
file, converts px/sz to floats on the server, and accumulates signed
notional: side `B` adds, side `A` subtracts. Payload is `sampled: true`, a
running `cvd` series plus `latest`, and the same young / `truncated` /
`window_capped` notes as prints. A missing trades file is **200** with a
note, not 404 — the sampler may be older than the board. An untracked coin
is **404**.

### Layer chips
Header chips `walls` `liq` `stops` `tp` `profile` `cvd`. Only walls, profile
and cvd do anything yet; liq / stops / tp stay visible and disabled, with a
methodology line that they need a later PR. Under L2, a compact list of
`/api/prints` (`≥ $25k`, `1h`) — notable prints, not the raw tape — with the
CVD spark under that list. Methodology: `HL only · sampled tape (last
~10/poll) · not a full book` describes CVD, not the prints list.

## Scope (PR8: panel ⑧ time × mark, one-screen layout)

The board now fits one screen at ~1400×900. Panel ⑧ is the large left
canvas; the OI × price matrix is a 280×260 side card with its readout and
funding under it; L2 depth and notable prints sit under ⑧. Levels ⑥ and
the volume profile ⑦ are below the fold. On desktop the L2 ladder reads
bids | asks side by side so all 30 levels land on the first screen; narrow
screens keep the stacked ladder and stack ⑧, matrix, book, funding.

### Mark path
`GET /api/marks/<COIN>?lookback=15m|1h|4h` (default `1h`) returns
`{ts, mark}` rows from the sampler's `oi_<COIN>.jsonl` — the same 12s mark,
not a new feed. Past 480 points the path is thinned by keeping each time
bucket's lowest and highest row, so a spike is never stepped over; every
point is still a raw sampler row, and `latest` is always the newest raw row.
A coin with no sampler file at all is **404**. A tracked coin with no oi
file yet, or fewer than two rows in the window, is **200** with a `note`
and no invented points; the page veils ⑧ until the path exists.
`/api/oi` keeps its contract.

### Panel ⑧
X is time over the chosen lookback, ending at serve time, so a short or
stale series reads as a short line. Y is mark price. Overlays share that
axis:
- **walls** — a horizontal line per tagged L2 level, stroke width scaled to
  the largest wall on screen; the largest per side is labelled with its
  notional. Repainted from the 2s book poll without refetching the path.
  The `walls` chip hides them here and un-bolds them on L2.
- **levels.yaml** — the same line styles as ⑥, labelled on the line. A
  level outside the plotted range is an edge tag (↑ / ↓), not a squashed
  axis.
- **volume** — the ⑦ profile as a thin histogram on the right edge, clipped
  to the plotted range and labelled `vol`. The `profile` chip hides it and ⑦.

stops / tp stay disabled; ⑧ draws no bands for them. liq is live from PR9.

## Scope (PR9: real liq map)

Real `liquidationPx` from Hyperliquid for open positions in the **200
largest accounts by account value** — the exchange's own number, not a
leverage-tier model of open interest. That set shows whale-sized fuel; it
misses crowded mid-size leverage outside the leaderboard top, and the page
says so. Hyperliquid only.

### Where the data comes from
The official client (outside this repo) gained `liqmap --coins BTC,ETH,HYPE,SOL
--top 200 --json`. It resolves the top-N addresses from Hyperliquid's public
leaderboard file (cached for 6h beside the client; it is ~37 MB), then reads
each account's `clearinghouseState` with a pool of five. One call covers
every coin — each account state already carries all of them. Rows with a
zero size or a null liq price are dropped. It never reads your own address.

The sampler runs it **in the background** every 120s and appends one line
per coin to `data/liq_<COIN>.jsonl` (`asof_ms`, `requested`, `fetched`,
`capped`, `positions`). The OI tick never waits on it: a liqmap still
running after 90s is killed and logged, and the 12s cadence holds.
Sampler flags: `--no-liq`, `--liq-interval <seconds>` (minimum 30).

### API
`GET /api/liq/<COIN>` reads the newest snapshot. Per side, positions whose
liq prices sit within **0.25%** of a cluster's lowest member form one
cluster: `{px, lo, hi, side, notional, wallets, positions}`, where `px` is
notional-weighted. Addresses are not served. Top level: `within_2pct` and
`within_5pct` (notional, long/short split, positions) measured from the
sampler's latest mark, `largest`, `coverage {requested, fetched, capped}`,
`age_s`, `bin_pct`, `method`. A coin with no sampler file is **404**; a
tracked coin with no liq file is **200** with empty clusters and a `note`.

### Panel
The `liq` chip is live. On ⑧ each cluster in range is a band over its price
span, opacity scaled to the largest in view, with a matching bar in the
right-edge strip beside `vol`. Clusters do not stretch the price axis; the
nearest one past each edge is a tag with its distance. The **liq map** card
under the matrix shows the 2% / 5% buckets, the largest cluster, and the
six clusters nearest mark, with coverage and age in its head.

## How it watches

The sampler invokes Hyperliquid's official info client as a subprocess every
12 seconds and appends the result to `data/oi_<COIN>.jsonl` and
`data/trades_<COIN>.jsonl`. The board reads those files for the OI quadrant
and notable prints, and calls the same client's live L2, funding, and 15m
candle endpoints on demand (cached between polls), with nothing else touching
the network. No keys, no orders, no alerts — ever. The doctrine
up top is not a slogan: the CI workflow in `.github/workflows/verify.yml` is
the rulebook, executed on every PR. It greps the code for an order path, any
credential-shaped name, a non-loopback bind, and signal language in the copy;
if any of those appears, the build fails and the pull request cannot be merged.

## Operations

- **Board daemon:** `./run_board.sh start|stop|status|restart|log`, mirroring
  `run_sampler.sh`. PID in `board.pid`, log in `data/board.log`. `start`
  refuses if port 8791 already answers, so a second server can never bind
  over a first; `stop` cleans up a stale PID file.
- **Port 8791:** chosen because 8765 belongs to another dashboard on this
  machine. 8791 is free; if it ever isn't, `python3 server.py --port <n>` takes
  a different loopback port.
- **Where data lives:** `data/` (gitignored). `oi_<COIN>.jsonl` is the OI
  history; `trades_<COIN>.jsonl` is the notable-prints log (last-10 polls,
  not a full tape). The process logs sit beside them.
- **Back it up:** Hyperliquid has no OI-history endpoint. Every hour the
  sampler records exists nowhere else but on this machine — `data/` is the
  only copy, and once the disk is gone the history is gone with it.
