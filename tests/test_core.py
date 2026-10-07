from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from eth_abi import encode

from stock_activity.contracts import ROBINHOOD_STOCK, UNISWAP_V3_POOL, UNISWAP_V4_POOL_MANAGER, event_topic
from stock_activity.decoders import decode_multiplier, decode_swap, decode_transfer, decode_v4_initialize
from stock_activity.process import associate_dividend, daily_transfers, transition_rows
from stock_activity.utils import hex_int


MULTIPLIER_TOPIC = event_topic(ROBINHOOD_STOCK.events.UIMultiplierUpdated)
TRANSFER_TOPIC = event_topic(ROBINHOOD_STOCK.events.Transfer)


def test_empty_explorer_hex_quantity_is_zero():
    assert hex_int("0x") == 0


def topic_address(value: str) -> str:
    return "0x" + value.removeprefix("0x").rjust(64, "0")


def log(data: bytes, topics: list[str | None], timestamp: int = 100, index: int = 0) -> dict:
    return {
        "address": "0x" + "99" * 20, "data": "0x" + data.hex(), "topics": topics,
        "transactionHash": "0x" + "ab" * 32, "transactionIndex": "0x0", "blockHash": "0x" + "cd" * 32,
        "logIndex": hex(index), "blockNumber": "0xa", "timeStamp": hex(timestamp),
    }


def test_explorer_null_topic_padding_is_ignored():
    zero = "0x" + "0" * 40
    alice = "0x" + "11" * 20
    item = log(
        encode(["uint256"], [1_000_000]),
        [TRANSFER_TOPIC, topic_address(zero), topic_address(alice), None],
    )
    assert decode_transfer(item, 4663, "AAPL", 6)["amount"] == "1"


def test_multiplier_decode_and_percentage():
    item = log(encode(["uint256", "uint256", "uint256"], [10**18, 101 * 10**16, 200]), [MULTIPLIER_TOPIC])
    row = decode_multiplier(item, 4663, "AAPL")
    assert row["old_multiplier"] == "1"
    assert row["new_multiplier"] == "1.01"
    assert Decimal(row["multiplier_change_pct"]) == 1
    assert row["effective_at"].endswith("+00:00")


def test_mint_burn_and_decimal_normalization():
    zero = "0x" + "0" * 40
    alice = "0x" + "11" * 20
    mint = decode_transfer(log(encode(["uint256"], [1_250_000]), [TRANSFER_TOPIC, topic_address(zero), topic_address(alice)]), 4663, "AAPL", 6)
    burn = decode_transfer(log(encode(["uint256"], [250_000]), [TRANSFER_TOPIC, topic_address(alice), topic_address(zero)], index=1), 4663, "AAPL", 6)
    ordinary = decode_transfer(log(encode(["uint256"], [1]), [TRANSFER_TOPIC, topic_address(alice), topic_address("0x" + "22" * 20)]), 4663, "AAPL", 6)
    assert mint["direction"] == "mint" and mint["amount"] == "1.25"
    assert burn["direction"] == "burn" and burn["amount"] == "0.25"
    assert ordinary is None
    daily = daily_transfers([mint, burn])
    assert daily[0]["net_issuance"] == Decimal("1.00")


@pytest.mark.parametrize("stock_is_token0", [True, False])
@pytest.mark.parametrize("version", ["v3", "v4"])
def test_swap_orderings_and_versions(stock_is_token0, version):
    stock, stable = "0x" + "11" * 20, "0x" + "22" * 20
    token0, token1 = (stock, stable) if stock_is_token0 else (stable, stock)
    # Trader buys 2 stock and pays 5 stable. V3 emits pool deltas while V4
    # emits the opposite BalanceDelta signs from the swapper's perspective.
    pool_amount0, pool_amount1 = ((-2_000_000, 5_000_000) if stock_is_token0 else (5_000_000, -2_000_000))
    amount0, amount1 = (pool_amount0, pool_amount1) if version == "v3" else (-pool_amount0, -pool_amount1)
    if version == "v3":
        item = log(
            encode(["int256", "int256", "uint160", "uint128", "int24"], [amount0, amount1, 1, 1, 0]),
            [event_topic(UNISWAP_V3_POOL.events.Swap), topic_address("0x" + "33" * 20), topic_address("0x" + "44" * 20)],
        )
    else:
        item = log(
            encode(["int128", "int128", "uint160", "uint128", "int24", "uint24"], [amount0, amount1, 1, 1, 0, 3000]),
            [event_topic(UNISWAP_V4_POOL_MANAGER.events.Swap), "0x" + "aa" * 32, topic_address("0x" + "33" * 20)],
        )
    row = decode_swap(item, 4663, "AAPL", version, token0, token1, stock, 6, 6)
    assert row["direction"] == "buy_stock"
    assert row["stock_bought"] == "2"
    assert row["stablecoin_bought"] == "0"
    assert row["amount0_raw"] == amount0 and row["amount1_raw"] == amount1


def test_v4_negative_stock_delta_is_a_stock_sale():
    stock, stable = "0x" + "11" * 20, "0x" + "22" * 20
    item = log(
        encode(["int128", "int128", "uint160", "uint128", "int24", "uint24"], [-2_000_000, 5_000_000, 1, 1, 0, 3000]),
        [event_topic(UNISWAP_V4_POOL_MANAGER.events.Swap), "0x" + "aa" * 32, topic_address("0x" + "33" * 20)],
    )
    row = decode_swap(item, 4663, "AAPL", "v4", stock, stable, stock, 6, 6)
    assert row["direction"] == "buy_stablecoin"
    assert row["stock_bought"] == "0"
    assert row["stablecoin_bought"] == "5"


def test_v4_initialize_uses_indexed_currencies():
    pool_id = "0x" + "aa" * 32
    token0, token1, hooks = "0x" + "11" * 20, "0x" + "22" * 20, "0x" + "33" * 20
    item = log(
        encode(["uint24", "int24", "address", "uint160", "int24"], [3000, 60, hooks, 123, -2]),
        [event_topic(UNISWAP_V4_POOL_MANAGER.events.Initialize), pool_id, topic_address(token0), topic_address(token1)],
    )
    row = decode_v4_initialize(item)
    assert row["pool_id"] == pool_id
    assert row["token0"] == token0 and row["token1"] == token1
    assert row["fee"] == 3000 and row["tick_spacing"] == 60 and row["hooks"] == hooks


def test_nearest_date_ex_date_wins_tie_and_missing_dividends():
    update = {"ticker": "AAPL", "effective_at": "2026-07-10T00:00:00+00:00"}
    dividends = [{
        "ticker": "AAPL", "ex_dividend_date": "2026-07-08", "payment_date": "2026-07-12", "amount": "0.25"
    }]
    association = associate_dividend(update, dividends)
    assert association["nearest_dividend_date_type"] == "ex_date"
    assert association["declared_dividend"] == "0.25"
    assert associate_dividend(update, [])["nearest_dividend_date"] == ""


def test_transition_boundary_inclusion():
    update = {"id": "u", "ticker": "AAPL", "emission_timestamp": 100, "effective_timestamp": 200}
    base = {"ticker": "AAPL"}
    transfers = [
        {**base, "id": "at-start", "timestamp": datetime.fromtimestamp(100, timezone.utc).isoformat()},
        {**base, "id": "before", "timestamp": datetime.fromtimestamp(199, timezone.utc).isoformat()},
        {**base, "id": "at-effective", "timestamp": datetime.fromtimestamp(200, timezone.utc).isoformat()},
    ]
    swaps = [
        {**base, "id": "at-start", "timestamp": datetime.fromtimestamp(100, timezone.utc).isoformat()},
        {**base, "id": "at-effective", "timestamp": datetime.fromtimestamp(200, timezone.utc).isoformat()},
        {**base, "id": "at-end", "timestamp": datetime.fromtimestamp(300, timezone.utc).isoformat()},
        {**base, "id": "outside", "timestamp": datetime.fromtimestamp(301, timezone.utc).isoformat()},
    ]
    mint_rows, swap_rows = transition_rows([update], transfers, swaps)
    assert [row["id"] for row in mint_rows] == ["at-start", "before"]
    assert [row["segment"] for row in swap_rows] == ["pre_effective", "post_effective", "post_effective"]
