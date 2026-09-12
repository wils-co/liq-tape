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
