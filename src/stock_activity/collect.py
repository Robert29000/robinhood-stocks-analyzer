from __future__ import annotations

import csv
import io
import json
import time as time_module
import tomllib
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

from web3 import HTTPProvider, Web3

from .clients import BlockscoutClient, HttpService, ServiceError
from .config import Settings
from .contracts import (
    ROBINHOOD_STOCK, UNISWAP_V3_POOL, UNISWAP_V4_POOL_MANAGER,
    contract_interface, event_topic,
)
from .decoders import decode_multiplier, decode_v4_initialize
from .rawstore import RawStore, atomic_json
from .utils import hex_int, parse_log_timestamp


def _midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _iter_assets(body: Any) -> Iterator[dict[str, Any]]:
    if isinstance(body, list):
        yield from (item for item in body if isinstance(item, dict))
    elif isinstance(body, dict):
        for key in ("assets", "results", "data"):
            value = body.get(key)
            if isinstance(value, list):
                yield from (item for item in value if isinstance(item, dict))
                return


def _web3_call(callable_: Any, retries: int) -> Any:
    error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            return callable_()
        except Exception as exc:  # Web3 providers surface several transport/RPC exception types.
            error = exc
            if attempt == retries:
                break
            time_module.sleep(min(0.5 * 2**attempt, 8))
    raise ServiceError(f"Web3 RPC call failed after {retries + 1} attempts: {error}")


class RequestPacer:
    def __init__(
        self,
        minimum_interval: float,
        *,
        clock: Callable[[], float] = time_module.monotonic,
        sleeper: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self.minimum_interval = minimum_interval
        self.clock = clock
        self.sleeper = sleeper
        self.last_request_at: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self.last_request_at is not None:
            remaining = self.minimum_interval - (now - self.last_request_at)
            if remaining > 0:
                self.sleeper(remaining)
                now = self.clock()
        self.last_request_at = now


def _fetch_alpha(
    settings: Settings,
    http: HttpService,
    store: RawStore,
    symbol: str,
    before_request: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    if not settings.alpha_vantage_api_key:
        raise ServiceError("ALPHAVANTAGE_API_KEY is required for collection")
    request = {"function": "DIVIDENDS", "symbol": symbol, "datatype": "csv"}
    params = {**request, "apikey": settings.alpha_vantage_api_key}
    cache_params = {"endpoint": settings.alpha_vantage_url, **request}
    payload = store.cached("alpha_vantage", cache_params, "csv")
    cache_hit = payload is not None
    if not cache_hit:
        if before_request is not None:
            before_request()
        response = http.get(settings.alpha_vantage_url, params)
        payload = response.content
    text = payload.decode("utf-8-sig")
    if text.lstrip().startswith("{"):
        raise ServiceError(f"Alpha Vantage returned an error for {symbol}: {text[:300]}")
    reader = csv.DictReader(io.StringIO(text))
    required = {"ex_dividend_date", "declaration_date", "record_date", "payment_date", "amount"}
    if not required.issubset(reader.fieldnames or []):
        raise ServiceError(f"Alpha Vantage returned malformed dividend CSV for {symbol}: {text[:300]}")
    rows = list(reader)
    if not cache_hit:
        store.save("alpha_vantage", cache_params, payload, "csv")
    kept = []
    for row in rows:
        ex_date = row.get("ex_dividend_date", "")
        try:
            parsed = date.fromisoformat(ex_date)
        except ValueError:
            continue
        if settings.ex_date_start <= parsed <= settings.ex_date_end:
            kept.append({"ticker": symbol, **row})
    return kept


def _fetch_assets(settings: Settings, http: HttpService, store: RawStore) -> tuple[Any, dict[str, dict[str, Any]]]:
    params = {"endpoint": settings.robinhood_assets_url, "chain_id": settings.chain_id}
    payload = store.cached("robinhood", params, "json")
    if payload is None:
        response = http.get(settings.robinhood_assets_url)
        payload = response.content
        store.save("robinhood", params, payload, "json")
    body = json.loads(payload)
    wanted = {ticker.symbol for ticker in settings.tickers}
    resolved: dict[str, dict[str, Any]] = {}
    for asset in _iter_assets(body):
        symbol = str(asset.get("tokenSymbol", asset.get("symbol", ""))).upper()
        if symbol not in wanted:
            continue
        deployments = asset.get("deployments", [])
        deployment = next((d for d in deployments if int(d.get("chainId", d.get("chain_id", -1))) == settings.chain_id), None)
        if deployment:
            address = str(deployment.get("contractAddress", deployment.get("address", ""))).lower()
            if len(address) == 42:
                resolved[symbol] = {"ticker": symbol, "token_address": address, "asset": asset}
    missing = wanted - resolved.keys()
    if missing:
        raise ServiceError(f"Robinhood /assets has no chain-{settings.chain_id} deployment for: {', '.join(sorted(missing))}")
    return body, resolved


def _timestamp_logs(web3: Web3, logs: list[dict[str, Any]], retries: int) -> None:
    timestamps: dict[int, int] = {}
    for log in logs:
        if log.get("timeStamp") is not None or log.get("timestamp") is not None:
            continue
        block_number = hex_int(log["blockNumber"])
        if block_number not in timestamps:
            block = _web3_call(lambda: web3.eth.get_block(block_number), retries)
            timestamps[block_number] = int(block["timestamp"])
        log["timeStamp"] = hex(timestamps[block_number])


def _pool_metadata(
    settings: Settings, web3: Web3, blockscout: BlockscoutClient,
    ticker: str, pool_type: str, pool_ref: str, stock_token: str, end: datetime,
) -> dict[str, Any]:
    if pool_type == "v3":
        contract = contract_interface(web3, "uniswap_v3_pool", pool_ref)
        token0 = str(_web3_call(contract.functions.token0().call, settings.retries)).lower()
        token1 = str(_web3_call(contract.functions.token1().call, settings.retries)).lower()
        fee = int(_web3_call(contract.functions.fee().call, settings.retries))
        metadata = {"token0": token0, "token1": token1, "fee": fee, "tick_spacing": "", "hooks": ""}
    else:
        last = blockscout.block_at(int(end.timestamp()) - 1, "before")
        initialize_topic = event_topic(UNISWAP_V4_POOL_MANAGER.events.Initialize)
        logs = blockscout.logs_blocks(settings.pool_manager, {"topic0": initialize_topic, "topic1": pool_ref}, 0, last)
        if len(logs) != 1:
            raise ServiceError(f"{ticker}: expected one PoolManager Initialize event for {pool_ref}, found {len(logs)}")
        metadata = decode_v4_initialize(logs[0])
        if metadata["pool_id"] != pool_ref:
            raise ServiceError(f"{ticker}: v4 Initialize pool id mismatch")
    expected = {stock_token.lower(), settings.stablecoin_address.lower()}
    actual = {metadata["token0"].lower(), metadata["token1"].lower()}
    if actual != expected:
        raise ServiceError(f"{ticker}: pool currencies {sorted(actual)} do not match stock/USDG {sorted(expected)}")
    return {
        "ticker": ticker, "pool_version": pool_type, "pool_address_or_id": pool_ref,
        "swap_emitter": pool_ref if pool_type == "v3" else settings.pool_manager,
        **metadata,
        "decimals0": int(_web3_call(contract_interface(web3, "erc20", metadata["token0"]).functions.decimals().call, settings.retries)),
        "decimals1": int(_web3_call(contract_interface(web3, "erc20", metadata["token1"]).functions.decimals().call, settings.retries)),
    }


def collect(settings: Settings) -> Path:
    if not settings.blockscout_api_key:
        raise ServiceError("BLOCKSCOUT_API_KEY is required for the Blockscout Pro API")
    started_at = datetime.now(timezone.utc).isoformat()
    config_snapshot = tomllib.loads(settings.config_path.read_text(encoding="utf-8"))
    store = RawStore(settings.raw_dir, config_snapshot)
    http = HttpService(settings.request_timeout, settings.retries)
    web3 = Web3(HTTPProvider(settings.rpc_url, request_kwargs={"timeout": settings.request_timeout}))
    blockscout = BlockscoutClient(
        settings.blockscout_url, settings.blockscout_api_key, settings.chain_id,
        store, settings.request_timeout, settings.retries,
    )
    alpha_pacer = RequestPacer(settings.alpha_vantage_request_delay)
    dividends: list[dict[str, Any]] = []
    for ticker in settings.tickers:
        dividends.extend(_fetch_alpha(settings, http, store, ticker.symbol, alpha_pacer.wait))
    _, tokens = _fetch_assets(settings, http, store)

    dates = [date.fromisoformat(row["ex_dividend_date"]) for row in dividends]
    payment_dates = []
    for row in dividends:
        try:
            payment_dates.append(date.fromisoformat(row.get("payment_date", "")))
        except ValueError:
            pass
    relevant = dates + payment_dates
    scan_start_date = (min(relevant) if relevant else settings.ex_date_start) - timedelta(days=settings.days_before_ex_date)
    scan_end_date = (max(relevant) if relevant else settings.ex_date_end) + timedelta(days=settings.days_after_effective + 1)
    scan_start = _midnight(scan_start_date)
    requested_scan_end = _midnight(scan_end_date)
    scan_end = min(requested_scan_end, datetime.now(timezone.utc) + timedelta(seconds=1))

    updates: list[dict[str, Any]] = []
    token_rows: list[dict[str, Any]] = []
    for ticker in settings.tickers:
        token = tokens[ticker.symbol]
        token_address = token["token_address"]
        token_contract = contract_interface(web3, "robinhood_stock_token", token_address)
        decimals = int(_web3_call(token_contract.functions.decimals().call, settings.retries))
        token_rows.append({
            "ticker": ticker.symbol, "token_address": token_address, "decimals": decimals,
            "asset_id": token["asset"].get("id", ""), "current_multiplier": token["asset"].get("currentMultiplier", ""),
        })
        multiplier_topic = event_topic(ROBINHOOD_STOCK.events.UIMultiplierUpdated)
        logs = blockscout.logs(token_address, {"topic0": multiplier_topic}, scan_start, scan_end)
        _timestamp_logs(web3, logs, settings.retries)
        for log in logs:
            row = decode_multiplier(log, settings.chain_id, ticker.symbol)
            row["emission_timestamp"] = int(parse_log_timestamp(log).timestamp())
            row["emitted_at"] = parse_log_timestamp(log).isoformat()
            now = datetime.now(timezone.utc)
            effective = datetime.fromtimestamp(row["effective_timestamp"], tz=timezone.utc)
            if effective <= now:
                block = blockscout.block_at(row["effective_timestamp"] - 1, "before")
                row["pre_effective_block"] = block
                row["total_supply_ui_raw"] = int(_web3_call(
                    lambda: token_contract.functions.totalSupplyUI().call(block_identifier=block), settings.retries
                ))
                row["total_supply_ui"] = str(Decimal(row["total_supply_ui_raw"]) / (Decimal(10) ** decimals))
                row["snapshot_status"] = "complete"
            else:
                row["pre_effective_block"] = ""
                row["total_supply_ui_raw"] = ""
                row["total_supply_ui"] = ""
                row["snapshot_status"] = "future_effective_time"
            updates.append(row)

    if updates:
        activity_start = min(scan_start, min(datetime.fromtimestamp(row["emission_timestamp"], timezone.utc) for row in updates))
        ends = []
        for row in updates:
            emission = datetime.fromtimestamp(row["emission_timestamp"], timezone.utc)
            effective = datetime.fromtimestamp(row["effective_timestamp"], timezone.utc)
            ends.append(max(effective + timedelta(days=settings.days_after_effective), effective + (effective - emission)))
        activity_end = min(max(scan_end, max(ends) + timedelta(seconds=1)), datetime.now(timezone.utc) + timedelta(seconds=1))
    else:
        activity_start, activity_end = scan_start, scan_end

    pools: list[dict[str, Any]] = []
    transfer_logs: list[dict[str, Any]] = []
    swap_logs: list[dict[str, Any]] = []
    for ticker in settings.tickers:
        token_row = next(row for row in token_rows if row["ticker"] == ticker.symbol)
        pool = _pool_metadata(settings, web3, blockscout, ticker.symbol, ticker.pool.type, ticker.pool.address,
                              token_row["token_address"], scan_end)
        pools.append(pool)
        transfer_topic = event_topic(ROBINHOOD_STOCK.events.Transfer)
        transfers = blockscout.logs(token_row["token_address"], {"topic0": transfer_topic}, activity_start, activity_end)
        _timestamp_logs(web3, transfers, settings.retries)
        transfer_logs.extend({"ticker": ticker.symbol, "log": item} for item in transfers)
        swap_interface = UNISWAP_V3_POOL if ticker.pool.type == "v3" else UNISWAP_V4_POOL_MANAGER
        topics = {"topic0": event_topic(swap_interface.events.Swap)}
        if ticker.pool.type == "v4":
            topics["topic1"] = ticker.pool.address
        swaps = blockscout.logs(pool["swap_emitter"], topics, activity_start, activity_end)
        _timestamp_logs(web3, swaps, settings.retries)
        swap_logs.extend({"ticker": ticker.symbol, "log": item} for item in swaps)

    collection = {
        "schema_version": 1, "chain_id": settings.chain_id,
        "scan_start": scan_start.isoformat(), "scan_end": scan_end.isoformat(),
        "activity_start": activity_start.isoformat(), "activity_end": activity_end.isoformat(),
        "dividends": dividends, "tokens": token_rows, "pools": pools, "multiplier_updates": updates,
        "transfer_logs": transfer_logs, "swap_logs": swap_logs,
    }
    path = settings.raw_dir / "collection.json"
    atomic_json(path, collection)
    store.record_run(started_at, {
        "collection_path": str(path.relative_to(settings.raw_dir)),
        "scan_start": scan_start.isoformat(), "scan_end": scan_end.isoformat(),
        "activity_start": activity_start.isoformat(), "activity_end": activity_end.isoformat(),
        "dividend_count": len(dividends), "multiplier_update_count": len(updates),
    })
    return path
