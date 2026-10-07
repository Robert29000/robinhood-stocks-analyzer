"""Opt-in service smoke tests: RUN_LIVE_TESTS=1 pytest -m live."""

import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from stock_activity.clients import RpcClient
from stock_activity.config import load_config
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
