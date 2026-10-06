from pathlib import Path

import pytest

from stock_activity.config import load_config


def test_checked_in_config_has_requested_pools(monkeypatch):
    monkeypatch.setenv("ROBINHOOD_RPC_URL", "https://rpc.example.invalid")
    settings = load_config(Path(__file__).parents[1] / "config.toml")
    assert settings.chain_id == 4663
    assert settings.rpc_url == "https://rpc.example.invalid"
    assert settings.blockscout_url == "https://api.blockscout.com/v2/api"
    assert settings.alpha_vantage_request_delay == 12
    assert [ticker.symbol for ticker in settings.tickers] == ["AAPL", "NVDA", "GOOGL", "MSFT", "META", "MU", "COST"]
    assert {ticker.symbol: ticker.pool.type for ticker in settings.tickers}["AAPL"] == "v4"
    assert settings.ex_date_start.isoformat() == "2026-07-01"
    assert settings.ex_date_end.isoformat() == "2026-09-30"


def test_invalid_pool_metadata_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBINHOOD_RPC_URL", "https://rpc.example.invalid")
    config = tmp_path / "bad.toml"
    config.write_text('''
[chain]
[[ticker]]
symbol = "AAPL"
pool = { type = "v2", address = "0x1111111111111111111111111111111111111111" }
''')
    with pytest.raises(ValueError, match="pool type"):
        load_config(config)


def test_rpc_url_environment_variable_is_required(tmp_path, monkeypatch):
    monkeypatch.delenv("ROBINHOOD_RPC_URL", raising=False)
    config = tmp_path / "missing-rpc.toml"
    config.write_text('''
[[ticker]]
symbol = "AAPL"
pool = { type = "v3", address = "0x1111111111111111111111111111111111111111" }
''')
    with pytest.raises(ValueError, match="ROBINHOOD_RPC_URL"):
        load_config(config)
