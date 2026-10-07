from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path
from typing import Any

from eth_utils import event_abi_to_log_topic
from hexbytes import HexBytes
from web3 import Web3

from .utils import hex_int


@lru_cache(maxsize=None)
def load_abi(name: str) -> list[dict[str, Any]]:
    filename = f"{name}.json"
    repository_file = Path(__file__).resolve().parents[2] / "abis" / filename
    if repository_file.exists():
        return json.loads(repository_file.read_text(encoding="utf-8"))
    # Wheels include the root-level ABI directory as package data.
    resource = files("stock_activity").joinpath("abis", filename)
    return json.loads(resource.read_text(encoding="utf-8"))


def contract_interface(web3: Web3, abi_name: str, address: str | None = None):
    checksum = Web3.to_checksum_address(address) if address else None
    return web3.eth.contract(address=checksum, abi=load_abi(abi_name))


def event_topic(event_factory: Any) -> str:
    event = event_factory()
    return Web3.to_hex(event_abi_to_log_topic(event.abi))


def as_web3_log(log: dict[str, Any]) -> dict[str, Any]:
    """Convert explorer JSON types to the LogReceipt expected by Web3.py."""
    zero_hash = "0x" + "00" * 32
    return {
        "address": Web3.to_checksum_address(log.get("address", "0x" + "00" * 20)),
        # Some Etherscan-compatible explorers pad their topics array with nulls.
        "topics": [HexBytes(value) for value in log.get("topics", []) if value is not None],
        "data": HexBytes(log.get("data", "0x")),
        "blockNumber": hex_int(log.get("blockNumber", 0)),
        "transactionHash": HexBytes(log.get("transactionHash", zero_hash)),
        "transactionIndex": hex_int(log.get("transactionIndex", 0)),
        "blockHash": HexBytes(log.get("blockHash", zero_hash)),
        "logIndex": hex_int(log.get("logIndex", 0)),
        "removed": bool(log.get("removed", False)),
    }


def decode_event(event_factory: Any, log: dict[str, Any]) -> dict[str, Any]:
    return dict(event_factory().process_log(as_web3_log(log))["args"])
ABI_WEB3 = Web3()
ROBINHOOD_STOCK = contract_interface(ABI_WEB3, "robinhood_stock_token")
ERC20 = contract_interface(ABI_WEB3, "erc20")
UNISWAP_V3_POOL = contract_interface(ABI_WEB3, "uniswap_v3_pool")
UNISWAP_V4_POOL_MANAGER = contract_interface(ABI_WEB3, "uniswap_v4_pool_manager")
