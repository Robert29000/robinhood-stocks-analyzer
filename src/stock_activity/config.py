from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


@dataclass(frozen=True)
class PoolConfig:
    type: str
    address: str


@dataclass(frozen=True)
class TickerConfig:
    symbol: str
    pool: PoolConfig


@dataclass(frozen=True)
class Settings:
    config_path: Path
    chain_id: int
    rpc_url: str
    blockscout_url: str
    pool_manager: str
    alpha_vantage_url: str
    robinhood_assets_url: str
    ex_date_start: date
    ex_date_end: date
    dividend_scan_padding_days: int
    days_before_effective: int
    days_after_effective: int
    raw_dir: Path
    output_dir: Path
    stablecoin_address: str
    tickers: tuple[TickerConfig, ...]
    alpha_vantage_api_key: str | None
    blockscout_api_key: str | None
    request_timeout: float = 30.0
    retries: int = 4
    alpha_vantage_request_delay: float = 12.0


def _required(table: dict[str, Any], key: str, section: str) -> Any:
    if key not in table:
        raise ValueError(f"missing [{section}].{key}")
    return table[key]


def _address(value: str, label: str, *, bytes32: bool = False) -> str:
    value = str(value).lower()
    expected = 66 if bytes32 else 42
    if not value.startswith("0x") or len(value) != expected:
        raise ValueError(f"invalid {label}: {value!r}")
    try:
        int(value[2:], 16)
    except ValueError as exc:
        raise ValueError(f"invalid {label}: {value!r}") from exc
    return value


def load_config(path: str | Path) -> Settings:
    config_path = Path(path).resolve()
    load_dotenv(config_path.parent / ".env", override=False)
    with config_path.open("rb") as fh:
        doc = tomllib.load(fh)
    chain = doc.get("chain", {})
    services = doc.get("services", {})
    window = doc.get("window", {})
    paths = doc.get("paths", {})
    base = config_path.parent
    chain_id = int(chain.get("id", 4663))
    rpc_url = os.getenv("ROBINHOOD_RPC_URL", "").strip()
    if not rpc_url:
        raise ValueError("ROBINHOOD_RPC_URL environment variable is required")
    ticker_rows = doc.get("ticker", [])
    if not ticker_rows:
        raise ValueError("at least one [[ticker]] is required")
    tickers: list[TickerConfig] = []
    seen: set[str] = set()
    for row in ticker_rows:
        symbol = str(_required(row, "symbol", "ticker")).upper()
        if symbol in seen:
            raise ValueError(f"duplicate ticker: {symbol}")
        seen.add(symbol)
        pool = _required(row, "pool", "ticker")
        pool_type = str(_required(pool, "type", "ticker.pool")).lower()
        if pool_type not in {"v3", "v4"}:
            raise ValueError(f"{symbol}: pool type must be v3 or v4")
        address = _address(_required(pool, "address", "ticker.pool"), f"{symbol} pool", bytes32=pool_type == "v4")
        tickers.append(TickerConfig(symbol, PoolConfig(pool_type, address)))
    start = date.fromisoformat(str(window.get("ex_date_start", "2026-07-01")))
    end = date.fromisoformat(str(window.get("ex_date_end", "2026-09-30")))
    if end < start:
        raise ValueError("ex_date_end precedes ex_date_start")
    raw_dir = (base / str(paths.get("raw_dir", "data/raw"))).resolve()
    output_dir = (base / str(paths.get("output_dir", "output"))).resolve()
    alpha_vantage_request_delay = float(services.get("alpha_vantage_request_delay", 12))
    if alpha_vantage_request_delay < 0:
        raise ValueError("[services].alpha_vantage_request_delay must be non-negative")
    dividend_scan_padding_days = int(window.get("dividend_scan_padding_days", 7))
    days_before_effective = int(window.get("days_before_effective", 7))
    days_after_effective = int(window.get("days_after_effective", 7))
    for key, value in (
        ("dividend_scan_padding_days", dividend_scan_padding_days),
        ("days_before_effective", days_before_effective),
        ("days_after_effective", days_after_effective),
    ):
        if value < 0:
            raise ValueError(f"[window].{key} must be non-negative")
    return Settings(
        config_path=config_path,
        chain_id=chain_id,
        rpc_url=rpc_url,
        blockscout_url=str(services.get("blockscout_url", "https://api.blockscout.com/v2/api")),
        pool_manager=_address(str(chain.get("pool_manager", "0x8366a39cc670b4001a1121b8f6a443a643e40951")), "pool manager"),
        alpha_vantage_url=str(services.get("alpha_vantage_url", "https://www.alphavantage.co/query")),
        robinhood_assets_url=str(services.get("robinhood_assets_url", "https://api.robinhood.com/rhj/assets")),
        ex_date_start=start,
        ex_date_end=end,
        dividend_scan_padding_days=dividend_scan_padding_days,
        days_before_effective=days_before_effective,
        days_after_effective=days_after_effective,
        raw_dir=raw_dir,
        output_dir=output_dir,
        stablecoin_address=_address(str(chain.get("stablecoin_address", "0x5fc5360D0400a0Fd4f2af552ADD042D716F1d168")), "stablecoin"),
        tickers=tuple(tickers),
        alpha_vantage_api_key=os.getenv("ALPHAVANTAGE_API_KEY"),
        blockscout_api_key=os.getenv("BLOCKSCOUT_API_KEY"),
        request_timeout=float(services.get("request_timeout", 30)),
        retries=int(services.get("retries", 4)),
        alpha_vantage_request_delay=alpha_vantage_request_delay,
    )
