from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

ZERO_ADDRESS = "0x" + "0" * 40


def hex_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    rendered = str(value)
    if rendered == "0x":
        return 0
    return int(rendered, 16) if rendered.startswith("0x") else int(rendered)


def utc_from_timestamp(timestamp: int) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)


def decimal_amount(raw: int, decimals: int) -> Decimal:
    return Decimal(raw) / (Decimal(10) ** decimals)


def log_id(chain_id: int, log: dict[str, Any]) -> str:
    tx = (log.get("transactionHash") or log.get("transaction_hash") or "").lower()
    index = hex_int(log.get("logIndex", log.get("index", 0)))
    return f"{chain_id}:{tx}:{index}"


def parse_log_timestamp(log: dict[str, Any]) -> datetime:
    value = log.get("timeStamp", log.get("timestamp"))
    if value is None:
        raise ValueError("log does not include timestamp")
    if isinstance(value, str) and ("T" in value or "-" in value):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc)
    return utc_from_timestamp(hex_int(value))
