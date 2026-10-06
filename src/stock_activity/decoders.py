from __future__ import annotations

from decimal import Decimal
from typing import Any

from web3 import Web3

from .contracts import ROBINHOOD_STOCK, UNISWAP_V3_POOL, UNISWAP_V4_POOL_MANAGER, decode_event
from .utils import ZERO_ADDRESS, decimal_amount, hex_int, log_id, parse_log_timestamp, utc_from_timestamp


def decode_multiplier(log: dict[str, Any], chain_id: int, ticker: str) -> dict[str, Any]:
    args = decode_event(ROBINHOOD_STOCK.events.UIMultiplierUpdated, log)
    old = int(args["oldMultiplier"])
    new = int(args["newMultiplier"])
    effective = int(args["effectiveAtTimestamp"])
    return {
        "id": log_id(chain_id, log), "ticker": ticker,
        "transaction_hash": log["transactionHash"].lower(),
        "log_index": hex_int(log["logIndex"]), "emission_block": hex_int(log["blockNumber"]),
        "old_multiplier_raw": old, "new_multiplier_raw": new,
        "old_multiplier": str(Decimal(old) / Decimal(10**18)),
        "new_multiplier": str(Decimal(new) / Decimal(10**18)),
        "effective_timestamp": effective,
        "effective_at": utc_from_timestamp(effective).isoformat(),
        "multiplier_change_pct": str((Decimal(new - old) / Decimal(old)) * 100) if old else "",
    }


def decode_transfer(log: dict[str, Any], chain_id: int, ticker: str, decimals: int) -> dict[str, Any] | None:
    args = decode_event(ROBINHOOD_STOCK.events.Transfer, log)
    source = str(args["from"]).lower()
    target = str(args["to"]).lower()
    if source != ZERO_ADDRESS and target != ZERO_ADDRESS:
        return None
    raw = int(args["value"])
    timestamp = parse_log_timestamp(log)
    return {
        "id": log_id(chain_id, log), "ticker": ticker,
        "transaction_hash": log["transactionHash"].lower(), "log_index": hex_int(log["logIndex"]),
        "block_number": hex_int(log["blockNumber"]), "timestamp": timestamp.isoformat(),
        "date": timestamp.date().isoformat(), "direction": "mint" if source == ZERO_ADDRESS else "burn",
        "from_address": source, "to_address": target, "amount_raw": raw,
        "amount": str(decimal_amount(raw, decimals)),
    }


def decode_swap(
    log: dict[str, Any], chain_id: int, ticker: str, version: str,
    token0: str, token1: str, stock_token: str, decimals0: int, decimals1: int,
) -> dict[str, Any]:
    interface = UNISWAP_V3_POOL if version == "v3" else UNISWAP_V4_POOL_MANAGER
    args = decode_event(interface.events.Swap, log)
    amount0_raw = int(args["amount0"])
    amount1_raw = int(args["amount1"])
    # V3 emits pool balance deltas. V4 emits BalanceDelta signs from the
    # swapper's perspective: negative is owed/input and positive is received/output.
    pool_amount0_raw = amount0_raw if version == "v3" else -amount0_raw
    pool_amount1_raw = amount1_raw if version == "v3" else -amount1_raw
    token0 = token0.lower()
    token1 = token1.lower()
    stock_token = stock_token.lower()
    if stock_token not in {token0, token1}:
        raise ValueError(f"{ticker}: pool does not contain configured stock token")
    stock_is_0 = stock_token == token0
    stock_delta_raw = pool_amount0_raw if stock_is_0 else pool_amount1_raw
    stable_delta_raw = pool_amount1_raw if stock_is_0 else pool_amount0_raw
    stock_decimals = decimals0 if stock_is_0 else decimals1
    stable_decimals = decimals1 if stock_is_0 else decimals0
    # Pool deltas: negative is sent out by the pool, hence bought by the swapper.
    direction = "buy_stock" if stock_delta_raw < 0 else "buy_stablecoin"
    timestamp = parse_log_timestamp(log)
    return {
        "id": log_id(chain_id, log), "ticker": ticker, "pool_version": version,
        "transaction_hash": log["transactionHash"].lower(), "log_index": hex_int(log["logIndex"]),
        "block_number": hex_int(log["blockNumber"]), "timestamp": timestamp.isoformat(),
        "date": timestamp.date().isoformat(), "direction": direction,
        "amount0_raw": amount0_raw, "amount1_raw": amount1_raw,
        "stock_delta": str(decimal_amount(stock_delta_raw, stock_decimals)),
        "stablecoin_delta": str(decimal_amount(stable_delta_raw, stable_decimals)),
        "stock_bought": str(decimal_amount(-stock_delta_raw, stock_decimals)) if stock_delta_raw < 0 else "0",
        "stablecoin_bought": str(decimal_amount(-stable_delta_raw, stable_decimals)) if stable_delta_raw < 0 else "0",
    }


def decode_v4_initialize(log: dict[str, Any]) -> dict[str, Any]:
    args = decode_event(UNISWAP_V4_POOL_MANAGER.events.Initialize, log)
    return {
        "pool_id": Web3.to_hex(args["id"]).lower(),
        "token0": str(args["currency0"]).lower(), "token1": str(args["currency1"]).lower(),
        "fee": int(args["fee"]), "tick_spacing": int(args["tickSpacing"]),
        "hooks": str(args["hooks"]).lower(),
        "block_number": hex_int(log["blockNumber"]),
    }
