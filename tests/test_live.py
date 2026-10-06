"""Opt-in service smoke tests: RUN_LIVE_TESTS=1 pytest -m live."""

import os
from pathlib import Path

import httpx
import pytest

from stock_activity.config import load_config


pytestmark = pytest.mark.live


@pytest.mark.skipif(os.getenv("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1")
def test_robinhood_assets_and_chain_rpc_are_reachable():
    settings = load_config(Path(__file__).parents[1] / "config.toml")
    assets = httpx.get("https://api.robinhood.com/rhj/assets", timeout=30)
    assets.raise_for_status()
    assert assets.json()
    response = httpx.post(settings.rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}, timeout=30)
    response.raise_for_status()
    assert int(response.json()["result"], 16) == 4663
