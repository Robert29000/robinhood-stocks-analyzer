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
from .utils import hex_int, log_id, parse_log_timestamp


ZERO_ADDRESS_TOPIC = "0x" + "0" * 64
Window = tuple[datetime, datetime]
Progress = Callable[[str, str], None]
COLLECT_STEPS = ("alpha", "assets", "logs")


def _resume_key(settings: Settings) -> dict[str, Any]:
    return {
        "chain_id": settings.chain_id,
        "ex_date_start": settings.ex_date_start.isoformat(),
        "ex_date_end": settings.ex_date_end.isoformat(),
        "tickers": [
            {"symbol": ticker.symbol, "pool_type": ticker.pool.type, "pool": ticker.pool.address}
            for ticker in settings.tickers
        ],
    }


def _save_checkpoint(
    settings: Settings,
    dividends: list[dict[str, Any]],
    tokens: list[dict[str, Any]] | None = None,
) -> None:
    checkpoint: dict[str, Any] = {
        "resume_key": _resume_key(settings),
        "dividends": dividends,
    }
    if tokens is not None:
        checkpoint["tokens"] = tokens
    atomic_json(settings.raw_dir / "collection-checkpoint.json", checkpoint)


def _load_checkpoint(settings: Settings, step: str) -> dict[str, Any]:
    checkpoint_path = settings.raw_dir / "collection-checkpoint.json"
    collection_path = settings.raw_dir / "collection.json"
    path = checkpoint_path if checkpoint_path.exists() else collection_path
    if not path.exists():
        raise ServiceError(f"cannot start from {step}: no prior collection checkpoint exists")
    try:
        checkpoint = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ServiceError(f"cannot read collection checkpoint: {path}") from exc
    if not isinstance(checkpoint, dict):
        raise ServiceError(f"collection checkpoint is malformed: {path}")

    saved_key = checkpoint.get("resume_key")
    if saved_key is not None and saved_key != _resume_key(settings):
        raise ServiceError("collection checkpoint does not match the current chain, window, or tickers")
    if not isinstance(checkpoint.get("dividends"), list):
        raise ServiceError("collection checkpoint does not contain Alpha Vantage dividends")
    if step == "logs":
        token_rows = checkpoint.get("tokens")
        expected = {ticker.symbol for ticker in settings.tickers}
        found = {row.get("ticker") for row in token_rows or [] if isinstance(row, dict)}
        if not isinstance(token_rows, list) or found != expected:
            raise ServiceError("collection checkpoint does not contain Robinhood assets for all tickers")
    return checkpoint


def _midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _merge_windows(windows: list[Window]) -> list[Window]:
    """Merge overlapping or touching half-open time windows."""
    merged: list[Window] = []
    for start, end in sorted(windows):
        if start >= end:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(merged[-1][1], end)
        else:
            merged.append((start, end))
    return merged


def _dividend_scan_windows(
    dividends: list[dict[str, Any]], padding_days: int, cutoff: datetime,
) -> tuple[list[dict[str, str]], dict[str, list[Window]]]:
    """Build per-dividend multiplier-discovery windows and merged ticker queries."""
    rows: list[dict[str, str]] = []
    by_ticker: dict[str, list[Window]] = {}
    padding = timedelta(days=padding_days)
    for dividend in dividends:
        ticker = str(dividend["ticker"])
        try:
            ex_date = date.fromisoformat(dividend["ex_dividend_date"])
            payment_date = date.fromisoformat(dividend["payment_date"])
        except (KeyError, ValueError) as exc:
            raise ServiceError(f"{ticker}: dividend has an invalid ex-date or payment date: {dividend}") from exc
        if payment_date < ex_date:
            raise ServiceError(
                f"{ticker}: dividend payment date {payment_date} precedes ex-date {ex_date}"
            )
        start = _midnight(ex_date) - padding
        requested_end = _midnight(payment_date) + padding + timedelta(days=1)
        fetch_end = min(requested_end, cutoff)
        rows.append({
            "ticker": ticker,
            "ex_dividend_date": ex_date.isoformat(),
            "payment_date": payment_date.isoformat(),
            "start": start.isoformat(),
            "end": requested_end.isoformat(),
            "fetch_end": fetch_end.isoformat(),
        })
        if start < fetch_end:
            by_ticker.setdefault(ticker, []).append((start, fetch_end))
    return rows, {ticker: _merge_windows(windows) for ticker, windows in by_ticker.items()}


def _event_activity_windows(
    updates: list[dict[str, Any]], days_before: int, days_after: int, cutoff: datetime,
) -> tuple[list[dict[str, str]], list[dict[str, str]], dict[str, list[Window]]]:
    """Build fixed effective-date and symmetric transition windows.

    Stored ends are inclusive event timestamps. Query ends are converted to
    half-open boundaries by adding one second, matching BlockscoutClient.logs.
    """
    activity_rows: list[dict[str, str]] = []
    transition_rows: list[dict[str, str]] = []
    queries: dict[str, list[Window]] = {}
    for update in updates:
        ticker = str(update["ticker"])
        emission = datetime.fromtimestamp(int(update["emission_timestamp"]), timezone.utc)
        effective = datetime.fromtimestamp(int(update["effective_timestamp"]), timezone.utc)
        if effective < emission:
            raise ServiceError(f"{ticker}: multiplier update is effective before it was emitted: {update['id']}")

        activity_start = effective - timedelta(days=days_before)
        activity_end = effective + timedelta(days=days_after)
        transition_start = emission
        transition_end = effective + (effective - emission)
        activity_rows.append({
            "update_id": str(update["id"]), "ticker": ticker,
            "start": activity_start.isoformat(), "end": activity_end.isoformat(),
        })
        transition_rows.append({
            "update_id": str(update["id"]), "ticker": ticker,
            "start": transition_start.isoformat(), "end": transition_end.isoformat(),
        })
        for start, inclusive_end in (
            (activity_start, activity_end),
            (transition_start, transition_end),
        ):
            fetch_end = min(inclusive_end + timedelta(seconds=1), cutoff)
            if start < fetch_end:
                queries.setdefault(ticker, []).append((start, fetch_end))
    return (
        activity_rows,
        transition_rows,
        {ticker: _merge_windows(windows) for ticker, windows in queries.items()},
    )


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
        retries = int(getattr(settings, "retries", 0))
        for attempt in range(retries + 1):
            if before_request is not None:
                before_request()
            response = http.get(settings.alpha_vantage_url, params)
            payload = response.content
            text = payload.decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            required = {"ex_dividend_date", "declaration_date", "record_date", "payment_date", "amount"}
            if not text.lstrip().startswith("{") and required.issubset(reader.fieldnames or []):
                break
            if attempt == retries:
                raise ServiceError(
                    f"Alpha Vantage returned an invalid response for {symbol} "
                    f"after {retries + 1} attempts: {text[:300]}"
                )
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


def _mint_burn_logs(
    blockscout: BlockscoutClient,
    chain_id: int,
    token_address: str,
    start: datetime,
    end: datetime,
) -> list[dict[str, Any]]:
    transfer_topic = event_topic(ROBINHOOD_STOCK.events.Transfer)
    found: dict[str, dict[str, Any]] = {}
    for indexed_address in ("topic1", "topic2"):
        topics = {"topic0": transfer_topic, indexed_address: ZERO_ADDRESS_TOPIC}
        for item in blockscout.logs(token_address, topics, start, end):
            found[log_id(chain_id, item)] = item
    return sorted(
        found.values(),
        key=lambda row: (hex_int(row.get("blockNumber", 0)), hex_int(row.get("logIndex", 0))),
    )


def _swap_topics(pool_type: str, pool_ref: str) -> dict[str, str]:
    swap_interface = UNISWAP_V3_POOL if pool_type == "v3" else UNISWAP_V4_POOL_MANAGER
    topics = {"topic0": event_topic(swap_interface.events.Swap)}
    if pool_type == "v4":
        topics["topic1"] = pool_ref
    return topics


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


def collect(
    settings: Settings,
    start_from: str = "alpha",
    progress: Progress | None = None,
) -> Path:
    if start_from not in COLLECT_STEPS:
        raise ValueError(f"unknown collection step: {start_from}")
    if not settings.blockscout_api_key:
        raise ServiceError("BLOCKSCOUT_API_KEY is required for the Blockscout Pro API")
    report = progress or (lambda phase, detail: None)
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
    checkpoint = _load_checkpoint(settings, start_from) if start_from != "alpha" else None

    if start_from == "alpha":
        dividends: list[dict[str, Any]] = []
        for index, ticker in enumerate(settings.tickers, 1):
            report("Alpha Vantage", f"{ticker.symbol} ({index}/{len(settings.tickers)})")
            dividends.extend(_fetch_alpha(settings, http, store, ticker.symbol, alpha_pacer.wait))
        _save_checkpoint(settings, dividends)
    else:
        dividends = checkpoint["dividends"]

    if start_from in {"alpha", "assets"}:
        report("Robinhood assets", "fetching token metadata")
        _, assets = _fetch_assets(settings, http, store)
        token_rows: list[dict[str, Any]] = []
        for ticker in settings.tickers:
            token = assets[ticker.symbol]
            token_address = token["token_address"]
            token_contract = contract_interface(web3, "robinhood_stock_token", token_address)
            decimals = int(_web3_call(token_contract.functions.decimals().call, settings.retries))
            token_rows.append({
                "ticker": ticker.symbol, "token_address": token_address, "decimals": decimals,
                "asset_id": token["asset"].get("id", ""),
                "current_multiplier": token["asset"].get("currentMultiplier", ""),
            })
        _save_checkpoint(settings, dividends, token_rows)
    else:
        token_rows = checkpoint["tokens"]
    token_by_ticker = {row["ticker"]: row for row in token_rows}

    collection_cutoff = datetime.now(timezone.utc) + timedelta(seconds=1)
    multiplier_scan_windows, multiplier_queries = _dividend_scan_windows(
        dividends, settings.dividend_scan_padding_days, collection_cutoff,
    )

    updates: list[dict[str, Any]] = []
    for index, ticker in enumerate(settings.tickers, 1):
        report("Chain logs", f"multiplier updates for {ticker.symbol} ({index}/{len(settings.tickers)})")
        token_row = token_by_ticker[ticker.symbol]
        token_address = token_row["token_address"]
        token_contract = contract_interface(web3, "robinhood_stock_token", token_address)
        decimals = int(token_row["decimals"])
        multiplier_topic = event_topic(ROBINHOOD_STOCK.events.UIMultiplierUpdated)
        found_logs: dict[str, dict[str, Any]] = {}
        for scan_start, scan_end in multiplier_queries.get(ticker.symbol, []):
            for log in blockscout.logs(token_address, {"topic0": multiplier_topic}, scan_start, scan_end):
                found_logs[log_id(settings.chain_id, log)] = log
        logs = sorted(
            found_logs.values(),
            key=lambda row: (hex_int(row.get("blockNumber", 0)), hex_int(row.get("logIndex", 0))),
        )
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

    activity_windows, transition_windows, activity_queries = _event_activity_windows(
        updates, settings.days_before_effective, settings.days_after_effective, collection_cutoff,
    )

    pools: list[dict[str, Any]] = []
    transfer_logs: list[dict[str, Any]] = []
    swap_logs: list[dict[str, Any]] = []
    for index, ticker in enumerate(settings.tickers, 1):
        report("Chain logs", f"activity for {ticker.symbol} ({index}/{len(settings.tickers)})")
        token_row = token_by_ticker[ticker.symbol]
        pool = _pool_metadata(settings, web3, blockscout, ticker.symbol, ticker.pool.type, ticker.pool.address,
                              token_row["token_address"], collection_cutoff)
        pools.append(pool)
        found_transfers: dict[str, dict[str, Any]] = {}
        topics = _swap_topics(ticker.pool.type, ticker.pool.address)
        found_swaps: dict[str, dict[str, Any]] = {}
        for activity_start, activity_end in activity_queries.get(ticker.symbol, []):
            for item in _mint_burn_logs(
                blockscout, settings.chain_id, token_row["token_address"], activity_start, activity_end,
            ):
                found_transfers[log_id(settings.chain_id, item)] = item
            for item in blockscout.logs(pool["swap_emitter"], topics, activity_start, activity_end):
                found_swaps[log_id(settings.chain_id, item)] = item
        transfers = sorted(
            found_transfers.values(),
            key=lambda row: (hex_int(row.get("blockNumber", 0)), hex_int(row.get("logIndex", 0))),
        )
        swaps = sorted(
            found_swaps.values(),
            key=lambda row: (hex_int(row.get("blockNumber", 0)), hex_int(row.get("logIndex", 0))),
        )
        _timestamp_logs(web3, transfers, settings.retries)
        _timestamp_logs(web3, swaps, settings.retries)
        transfer_logs.extend({"ticker": ticker.symbol, "log": item} for item in transfers)
        swap_logs.extend({"ticker": ticker.symbol, "log": item} for item in swaps)

    collection = {
        "schema_version": 2, "chain_id": settings.chain_id,
        "resume_key": _resume_key(settings),
        "collection_cutoff": collection_cutoff.isoformat(),
        "multiplier_scan_windows": multiplier_scan_windows,
        "activity_windows": activity_windows, "transition_windows": transition_windows,
        "dividends": dividends, "tokens": token_rows, "pools": pools, "multiplier_updates": updates,
        "transfer_logs": transfer_logs, "swap_logs": swap_logs,
    }
    path = settings.raw_dir / "collection.json"
    report("Saving", "collection.json")
    atomic_json(path, collection)
    store.record_run(started_at, {
        "collection_path": str(path.relative_to(settings.raw_dir)),
        "dividend_count": len(dividends), "multiplier_update_count": len(updates),
        "multiplier_scan_window_count": len(multiplier_scan_windows),
        "activity_window_count": len(activity_windows),
    })
    return path
