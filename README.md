# Robinhood Stock Dividend Activity Analyzer

This project links stock dividend dates to activity on Robinhood Chain. It analyzes these records:

- Stock-token multiplier updates
- Token mint and burn events
- Swaps in configured Uniswap V3 and V4 USDG pools

The collector stores source responses before it processes them. The processor creates CSV files for analysis.

All dates and timestamps use UTC.

## Requirements

- Python 3.11 or a later version
- An Alpha Vantage API key
- A Blockscout Pro API key
- An archive-capable Robinhood Chain RPC endpoint

The archive RPC must support historical contract calls. The collector uses these calls for pre-effective supply snapshots.

## Installation

1. Create and activate a virtual environment.

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Install the package and the optional development dependencies.

   ```bash
   pip install -e '.[test,notebook]'
   ```

3. Create the environment file.

   ```bash
   cp .env.example .env
   ```

4. Add these values to `.env`:

   ```dotenv
   ROBINHOOD_RPC_URL=
   ALPHAVANTAGE_API_KEY=
   BLOCKSCOUT_API_KEY=
   ```

The CLI reads `.env` from the directory that contains the selected configuration file.

## Configuration

Edit `config.toml` before collection.

The checked-in configuration contains seven stock symbols and their USDG pools. The application does not discover pools.

### Date filter

These fields select Alpha Vantage dividend rows by ex-dividend date:

```toml
[window]
ex_date_start = "2026-07-01"
ex_date_end = "2026-09-30"
```

The start and end dates are inclusive.

### Collection windows

The collector uses two ranges.

1. The dividend scan range applies to each dividend.

   ```text
   start = ex-dividend date - dividend_scan_padding_days
   end   = payment date + dividend_scan_padding_days
   ```

   The collector includes the complete end date. It searches this range for
   `UIMultiplierUpdated`, mint, and burn events.

2. The swap range applies to each multiplier update.

   ```text
   difference = effective time - emission time
   start      = emission time - difference
   end        = effective time + (2 * difference)
   ```

   The main swap CSV files use this full range. The transition CSV keeps the
   emission-to-post-effective subset for focused comparison.

Configure the window sizes in `config.toml`:

```toml
[window]
dividend_scan_padding_days = 7
```

The collector merges overlapping ranges for each ticker. This merge prevents duplicate requests.

Multiplier, mint, and burn requests start with the complete merged range.
Blockscout ranges that reach the 1,000-log response limit are divided by block
until each response is below the limit. Swap requests use daily UTC chunks and
the same limit-based splitting. The collector removes duplicate logs after collection.

### Services

The `[services]` section controls service URLs, timeouts, retries, and Alpha Vantage request spacing.

The collector waits only before an uncached Alpha Vantage request. It does not wait before it reads a cached response.
Blockscout network requests start at least 0.3 seconds apart. Existing retry backoff applies after errors.

## Run the application

Run collection and processing in one command:

```bash
stock-activity collect process --config config.toml
```

The CLI completes collection before it starts processing. You can also run one action:

```bash
stock-activity collect --config config.toml
stock-activity process --config config.toml
```

Collection shows the current source and ticker on one terminal line. To resume
from a completed phase after an interruption, select its next start point:

```bash
stock-activity collect --from assets --config config.toml
stock-activity collect --from logs --config config.toml
```

The available start points are `alpha` (the default), `assets`, and `logs`.
The `logs` step includes multiplier, mint/burn, and swap log collection. The
collector saves `data/raw/collection-checkpoint.json` after the Alpha and asset
phases. A checkpoint must match the current chain, date window, and tickers.

The `process` action requires a complete `data/raw/collection.json` file.

## Data collection

The collector uses these sources:

- Alpha Vantage supplies dividend CSV data.
- The Robinhood assets endpoint supplies token deployments and current multipliers.
- Blockscout supplies blocks and contract event logs.
- The Robinhood Chain RPC supplies contract metadata, block timestamps, and historical supply values.

Web3.py decodes events with the contract ABIs in `abis`. The project does not use manual event-byte parsing.

### Raw data and cache

The collector stores source responses in `data/raw`. It identifies each cached response from its source and request parameters.

`data/raw/manifest.json` records this information:

- Request parameters
- Collection times
- Configuration snapshots
- SHA-256 checksums
- Completed collection metadata

API keys and the RPC URL do not appear in cached request identities.

The collector reuses a cached response when its request identity matches a prior request. This behavior makes an interrupted collection resumable.

## Processed output

The processor writes CSV files to the configured output directory.

| File | Content |
|---|---|
| `dividends.csv` | Filtered dividend records |
| `tokens.csv` | Stock-token addresses, decimals, and asset metadata |
| `pools.csv` | Pool addresses, currencies, fees, and decimals |
| `multiplier_updates.csv` | Decoded multiplier updates and nearest dividend dates |
| `mint_burn_events.csv` | Mint and burn events in dividend scan ranges |
| `mint_burn_daily.csv` | Daily mint, burn, and net issuance totals |
| `mint_burn_transitions.csv` | Mint and burn events from emission until effectiveness |
| `swaps.csv` | Decoded swaps in the full multiplier-update swap ranges |
| `swaps_daily.csv` | Daily swap counts and amounts |
| `swap_transitions.csv` | Swaps from emission through the mirrored post-effective period |

The processor writes each file atomically.

### Event conventions

- A mint is an ERC-20 transfer from the zero address.
- A burn is an ERC-20 transfer to the zero address.
- Swap amounts use pool delta signs.
- A negative stock delta means that the trader bought stock.
- A negative USDG delta means that the trader bought USDG.

The collector keeps multiplier updates with future effective times. These rows use `snapshot_status=future_effective_time`.

The collector leaves the pre-effective supply fields empty for these rows. A later collection can create the snapshot.

## Analysis notebook

Run the notebook after the processor creates the CSV files:

```bash
jupyter execute notebooks/analysis.ipynb
```

The notebook reads only processed CSV files. It downloads Yahoo Finance daily prices into memory.

Set `STOCK_ACTIVITY_OUTPUT_DIR` to select a different output directory.

Set `STOCK_ACTIVITY_YAHOO_FIXTURE` for deterministic offline prices. Use this JSON structure:

```json
{
  "AAPL": {
    "2026-08-10": 100.0
  }
}
```

The notebook reads the fixture into memory. It does not copy the fixture into `data/raw`.

## Tests

Run the unit tests:

```bash
pytest
```

Run the live service test:

```bash
RUN_LIVE_TESTS=1 pytest -m live
```

The live test checks the Robinhood assets endpoint and the configured chain RPC.
