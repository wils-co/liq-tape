# liq-tape

Real-time, read-only liquidity and open interest instrument panel for discretionary trading.

## Doctrine
- **Read-only forever:** Surfaces market data, structure, and open interest regimes. Never generates trade cues, alerts, or signals.
- **No orders:** Zero order placement or execution capabilities. Wilson's discretion is the trading brain; this dashboard is the instrument panel.
- **No keys:** Zero credential or signing infrastructure. No private keys, secret files, or external trading permissions.
- **No signals:** Displays raw and regime-contextualized data without buy/sell bias.

## Scope (PR1: Foundation Sampler)
PR1 provides the local background sampler recording BTC and ETH perpetual market contexts (mark price, open interest, funding rate, and premium) into append-only JSONL files.

- Data source: the Hyperliquid official info client (`hyperliquid_client.py markets --json`), invoked as a subprocess. Point `--client` at your local copy of the official client.
- Target files: `data/oi_BTC.jsonl`, `data/oi_ETH.jsonl`
- Polling cadence: Configurable (default 12s)

## Usage

### Run directly
```bash
python3 sampler.py
```

Options:
- `--interval <seconds>`: Polling interval in seconds (default: `12.0`)
- `--data-dir <path>`: Directory where `.jsonl` files are stored (default: `data/`)
- `--coins <COIN1,COIN2,...>`: Comma-separated list of coins (default: `BTC,ETH`)
- `--client <path>`: Path to `hyperliquid_client.py`

### Run as background daemon
```bash
./run_sampler.sh start   # Starts sampler via nohup
./run_sampler.sh status  # Checks running status
./run_sampler.sh stop    # Stops background process
./run_sampler.sh log     # Follows log output
```

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
BTC | ETH toggle, last price, and a data-age badge (amber past 60s, red and marked
`STALE` past 300s). The quadrant plots price delta on X against open-interest delta on
Y, polling every 5s. The four regimes — trend (OI up, price up), squeeze (OI down,
price up), fresh shorts (OI up, price down), long flush (OI down, price down) — are
rendered as lens text in the corners. They are reading aids, not instructions: the
dot's position is the whole of the information.
