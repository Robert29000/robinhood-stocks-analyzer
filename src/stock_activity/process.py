from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .config import Settings
from .decoders import decode_swap, decode_transfer
from .rawstore import write_csv


DIVIDEND_FIELDS = ["ticker", "ex_dividend_date", "declaration_date", "record_date", "payment_date", "amount"]
TOKEN_FIELDS = ["ticker", "token_address", "decimals", "asset_id", "current_multiplier"]
POOL_FIELDS = ["ticker", "pool_version", "pool_address_or_id", "swap_emitter", "token0", "token1", "fee", "tick_spacing", "hooks", "decimals0", "decimals1"]
UPDATE_FIELDS = [
    "id", "ticker", "transaction_hash", "log_index", "emission_block", "emission_timestamp", "emitted_at",
    "old_multiplier_raw", "new_multiplier_raw", "old_multiplier", "new_multiplier", "effective_timestamp", "effective_at",
    "multiplier_change_pct", "pre_effective_block", "total_supply_ui_raw", "total_supply_ui", "snapshot_status",
    "nearest_dividend_date", "nearest_dividend_date_type", "nearest_dividend_days", "declared_dividend",
    "nearest_ex_date", "nearest_ex_date_days", "nearest_payment_date", "nearest_payment_date_days",
]
TRANSFER_FIELDS = ["id", "ticker", "transaction_hash", "log_index", "block_number", "timestamp", "date", "direction", "from_address", "to_address", "amount_raw", "amount"]
TRANSFER_DAILY_FIELDS = ["ticker", "date", "mint_count", "mint_amount", "burn_count", "burn_amount", "net_issuance"]
SWAP_FIELDS = ["id", "ticker", "pool_version", "transaction_hash", "log_index", "block_number", "timestamp", "date", "direction", "amount0_raw", "amount1_raw", "stock_delta", "stablecoin_delta", "stock_bought", "stablecoin_bought"]
SWAP_DAILY_FIELDS = ["ticker", "date", "total_swaps", "buy_stock_count", "buy_stablecoin_count", "stock_bought", "stablecoin_bought"]


def associate_dividend(update: dict[str, Any], dividends: list[dict[str, Any]]) -> dict[str, Any]:
    effective = datetime.fromisoformat(update["effective_at"]).date()
    candidates = [row for row in dividends if row["ticker"] == update["ticker"]]
    result = {
        "nearest_dividend_date": "", "nearest_dividend_date_type": "", "nearest_dividend_days": "",
        "declared_dividend": "", "nearest_ex_date": "", "nearest_ex_date_days": "",
        "nearest_payment_date": "", "nearest_payment_date_days": "",
    }
    choices: list[tuple[int, int, date, str, dict[str, Any]]] = []
    for row in candidates:
        for tie_priority, (key, kind) in enumerate((("ex_dividend_date", "ex_date"), ("payment_date", "payment_date"))):
            try:
                when = date.fromisoformat(row.get(key, ""))
            except ValueError:
                continue
            choices.append((abs((effective - when).days), tie_priority, when, kind, row))
    if choices:
        distance, _, when, kind, row = min(choices, key=lambda item: (item[0], item[1], item[2]))
        result.update({
            "nearest_dividend_date": when.isoformat(), "nearest_dividend_date_type": kind,
            "nearest_dividend_days": distance, "declared_dividend": row.get("amount", ""),
        })
    for key, output_date, output_days in (
        ("ex_dividend_date", "nearest_ex_date", "nearest_ex_date_days"),
        ("payment_date", "nearest_payment_date", "nearest_payment_date_days"),
    ):
        dated = []
        for row in candidates:
            try:
                when = date.fromisoformat(row.get(key, ""))
            except ValueError:
                continue
            dated.append((abs((effective - when).days), when))
        if dated:
            distance, when = min(dated, key=lambda item: (item[0], item[1]))
            result[output_date], result[output_days] = when.isoformat(), distance
    return result


def daily_transfers(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        key = event["ticker"], event["date"]
        row = grouped.setdefault(key, {
            "ticker": key[0], "date": key[1], "mint_count": 0, "mint_amount": Decimal(0),
            "burn_count": 0, "burn_amount": Decimal(0), "net_issuance": Decimal(0),
        })
        amount = Decimal(event["amount"])
        if event["direction"] == "mint":
            row["mint_count"] += 1
            row["mint_amount"] += amount
            row["net_issuance"] += amount
        else:
            row["burn_count"] += 1
            row["burn_amount"] += amount
            row["net_issuance"] -= amount
    return [grouped[key] for key in sorted(grouped)]


def daily_swaps(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for event in events:
        key = event["ticker"], event["date"]
        row = grouped.setdefault(key, {
            "ticker": key[0], "date": key[1], "total_swaps": 0, "buy_stock_count": 0,
            "buy_stablecoin_count": 0, "stock_bought": Decimal(0), "stablecoin_bought": Decimal(0),
        })
        row["total_swaps"] += 1
        row[f"{event['direction']}_count"] += 1
        row["stock_bought"] += Decimal(event["stock_bought"])
        row["stablecoin_bought"] += Decimal(event["stablecoin_bought"])
    return [grouped[key] for key in sorted(grouped)]


def transition_rows(
    updates: list[dict[str, Any]], transfers: list[dict[str, Any]], swaps: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    transfer_output: list[dict[str, Any]] = []
    swap_output: list[dict[str, Any]] = []
    for update in updates:
        emitted = int(update["emission_timestamp"])
        effective = int(update["effective_timestamp"])
        mirrored_end = effective + (effective - emitted)
        for event in transfers:
            timestamp = int(datetime.fromisoformat(event["timestamp"]).timestamp())
            if event["ticker"] == update["ticker"] and emitted <= timestamp < effective:
                transfer_output.append({"update_id": update["id"], "segment": "pre_effective", **event})
        for event in swaps:
            timestamp = int(datetime.fromisoformat(event["timestamp"]).timestamp())
            if event["ticker"] == update["ticker"] and emitted <= timestamp <= mirrored_end:
                segment = "pre_effective" if timestamp < effective else "post_effective"
                swap_output.append({
                    "update_id": update["id"], "segment": segment,
                    "window_start": datetime.fromtimestamp(emitted, timezone.utc).isoformat(),
                    "window_effective": datetime.fromtimestamp(effective, timezone.utc).isoformat(),
                    "window_end": datetime.fromtimestamp(mirrored_end, timezone.utc).isoformat(), **event,
                })
    return transfer_output, swap_output


def process(settings: Settings) -> list[Path]:
    collection_path = settings.raw_dir / "collection.json"
    if not collection_path.exists():
        raise FileNotFoundError(f"no collection found at {collection_path}; run collect first")
    data = json.loads(collection_path.read_text())
    token_by_ticker = {row["ticker"]: row for row in data["tokens"]}
    pool_by_ticker = {row["ticker"]: row for row in data["pools"]}
    transfers: list[dict[str, Any]] = []
    for wrapped in data["transfer_logs"]:
        token = token_by_ticker[wrapped["ticker"]]
        event = decode_transfer(wrapped["log"], data["chain_id"], wrapped["ticker"], int(token["decimals"]))
        if event is not None:
            transfers.append(event)
    swaps: list[dict[str, Any]] = []
    for wrapped in data["swap_logs"]:
        ticker = wrapped["ticker"]
        pool, token = pool_by_ticker[ticker], token_by_ticker[ticker]
        swaps.append(decode_swap(
            wrapped["log"], data["chain_id"], ticker, pool["pool_version"], pool["token0"], pool["token1"],
            token["token_address"], int(pool["decimals0"]), int(pool["decimals1"]),
        ))
    transfers.sort(key=lambda row: (row["ticker"], row["block_number"], row["log_index"]))
    swaps.sort(key=lambda row: (row["ticker"], row["block_number"], row["log_index"]))
    updates = []
    for update in data["multiplier_updates"]:
        updates.append({**update, **associate_dividend(update, data["dividends"])})
    mint_transitions, swap_transitions = transition_rows(updates, transfers, swaps)

    outputs: list[tuple[str, list[dict[str, Any]], list[str]]] = [
        ("dividends.csv", data["dividends"], DIVIDEND_FIELDS), ("tokens.csv", data["tokens"], TOKEN_FIELDS),
        ("pools.csv", data["pools"], POOL_FIELDS), ("multiplier_updates.csv", updates, UPDATE_FIELDS),
        ("mint_burn_events.csv", transfers, TRANSFER_FIELDS),
        ("mint_burn_daily.csv", daily_transfers(transfers), TRANSFER_DAILY_FIELDS),
        ("swaps.csv", swaps, SWAP_FIELDS), ("swaps_daily.csv", daily_swaps(swaps), SWAP_DAILY_FIELDS),
        ("mint_burn_transitions.csv", mint_transitions, ["update_id", "segment", *TRANSFER_FIELDS]),
        ("swap_transitions.csv", swap_transitions, ["update_id", "segment", "window_start", "window_effective", "window_end", *SWAP_FIELDS]),
    ]
    written = []
    for filename, rows, fields in outputs:
        path = settings.output_dir / filename
        write_csv(path, rows, fields)
        written.append(path)
    return written
