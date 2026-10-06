# Robinhood Stock Dividend Activity Analyzer

This project collects dividend declarations and Robinhood Chain activity, then produces analysis-ready CSV files correlating stock-token multiplier changes, issuance/redemption, and swaps in fixed Uniswap USDG pools. All application timestamps and date boundaries are UTC. Yahoo Finance is deliberately isolated to the notebook.

Onchain data is decoded by Web3.py from vendored public contract ABIs—there is no hand-written event byte parsing.

## Setup

Python 3.11 or newer is required.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[test,notebook]'
cp .env.example .env
# Fill in ROBINHOOD_RPC_URL, ALPHAVANTAGE_API_KEY, and BLOCKSCOUT_API_KEY in .env.
# ROBINHOOD_RPC_URL should use an archive-capable endpoint for reliable historical snapshots.
```

The checked-in [config.toml](config.toml) includes the seven requested highest-TVL USDG pools and official Robinhood Chain defaults. Pool discovery is intentionally out of scope.

```bash
stock-activity collect --config config.toml
stock-activity process --config config.toml
```

`collect` is resumable: request identities are stable, original Alpha Vantage CSV, Robinhood JSON, and every Blockscout response are retained below `data/raw`, and `manifest.json` records parameters, collection time, the configuration snapshot, and SHA-256 checksums. Contract reads need an archive-capable RPC to snapshot `totalSupplyUI()` at the last block strictly before multiplier effectiveness.

The CLI automatically loads `.env` beside the selected configuration file. The RPC endpoint is read from `ROBINHOOD_RPC_URL` and is not stored in the TOML configuration snapshot. Blockscout requests use the unified Pro endpoint `https://api.blockscout.com/v2/api` with `chainid=4663` and the API key supplied as `apikey`.

`process` atomically writes these files below `output`:

- `dividends.csv`, `tokens.csv`, `pools.csv`, `multiplier_updates.csv`
- `mint_burn_events.csv`, `mint_burn_daily.csv`, `mint_burn_transitions.csv`
- `swaps.csv`, `swaps_daily.csv`, `swap_transitions.csv`

Mint and burn mean ERC-20 transfers from or to the zero address. Swap signs are pool deltas: a negative stock delta means the trader bought stock; a negative USDG delta means the trader bought stablecoin.

## Notebook

Run `notebooks/analysis.ipynb` after processing. It reads only the processed CSVs and downloads Yahoo daily prices in memory. Set `STOCK_ACTIVITY_OUTPUT_DIR` to use a non-default output directory. For deterministic/offline execution, set `STOCK_ACTIVITY_YAHOO_FIXTURE` to a JSON object mapping each ticker to `{"YYYY-MM-DD": close}`; the fixture is read into memory and is never copied into the raw data contract.

```bash
jupyter execute notebooks/analysis.ipynb
pytest
```

Future multiplier effective times are retained with `snapshot_status=future_effective_time`; their pre-effective supply snapshot stays blank until a later collection run.
