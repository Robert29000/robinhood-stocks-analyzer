"""Opt-in service smoke tests: RUN_LIVE_TESTS=1 pytest -m live."""

import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from stock_activity.clients import ExplorerClient, RpcClient
from stock_activity.config import load_config
from stock_activity.contracts import UNISWAP_V4_POOL_MANAGER, event_topic
from stock_activity.rawstore import RawStore


pytestmark = pytest.mark.live


@pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")
def test_robinhood_assets_and_chain_rpc_are_reachable(tmp_path):
    settings = load_config(Path(__file__).parents[1] / "config.toml")
    assets = httpx.get("https://api.robinhood.com/rhj/assets", timeout=30)
    assets.raise_for_status()
    assert assets.json()
    rpc = RpcClient(
        settings.rpc_url,
        settings.chain_id,
        RawStore(tmp_path, {}),
        SimpleNamespace(),
        settings.request_timeout,
        settings.retries,
    )
    assert int(rpc._query("eth_chainId", [])["result"], 16) == settings.chain_id
    latest = int(rpc._query("eth_blockNumber", [])["result"], 16)
    logs = rpc.logs_blocks(settings.pool_manager, {}, max(0, latest - 10), latest)
    assert isinstance(logs, list)

    explorer = ExplorerClient(
        settings.explorer_url,
        settings.explorer_api_key,
        settings.chain_id,
        RawStore(tmp_path / "explorer", {}),
        settings.request_timeout,
        settings.retries,
        settings.explorer_request_delay,
        settings.explorer_log_limit,
    )
    block = explorer.block_at(1782864000, "after")
    assert block > 0
    swap_topic = event_topic(UNISWAP_V4_POOL_MANAGER.events.Swap)
    explorer_logs = explorer.logs_blocks(
        settings.pool_manager, {"topic0": swap_topic}, max(0, latest - 10), latest,
    )
    assert isinstance(explorer_logs, list)
