from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from stock_activity.clients import RequestPacer, RpcClient, ServiceError


def make_log(block, index=0):
    return {
        "blockNumber": hex(block),
        "logIndex": hex(index),
        "transactionHash": "0x" + f"{block:064x}",
    }


def client_with(query):
    client = object.__new__(RpcClient)
    client.chain_id = 4663
    client._query = query
    return client


def test_rpc_topics_preserve_empty_indexed_positions():
    assert RpcClient._rpc_topics({"topic0": "0xevent", "topic2": "0xaddress"}) == [
        "0xevent", None, "0xaddress",
    ]


def test_rpc_log_filter_uses_hex_blocks_and_ten_thousand_limit():
    calls = []

    def query(method, params):
        calls.append((method, params))
        return {"jsonrpc": "2.0", "result": [make_log(10)]}

    client = client_with(query)
    assert client._logs_range("0xABC", {"topic0": "0xtopic"}, 10, 20) == [make_log(10)]
    assert client.LIMIT == 10_000
    assert calls == [("eth_getLogs", [{
        "address": "0xabc",
        "fromBlock": "0xa",
        "toBlock": "0x14",
        "topics": ["0xtopic"],
    }])]


def test_rpc_exact_limit_recursively_splits():
    calls = []

    def query(method, params):
        start = int(params[0]["fromBlock"], 16)
        end = int(params[0]["toBlock"], 16)
        calls.append((start, end))
        if (start, end) == (1, 2):
            return {"result": [make_log(1, index) for index in range(10_000)]}
        return {"result": [make_log(start)]}

    client = client_with(query)
    assert len(client._logs_range("0x1", {}, 1, 2)) == 2
    assert calls == [(1, 2), (1, 1), (2, 2)]


def test_rpc_ranges_over_ten_thousand_blocks_split_before_request():
    calls = []

    def query(method, params):
        start = int(params[0]["fromBlock"], 16)
        end = int(params[0]["toBlock"], 16)
        calls.append((start, end))
        return {"result": []}

    client = client_with(query)
    assert client._logs_range("0x1", {}, 1, 20_000) == []
    assert calls == [(1, 10_000), (10_001, 20_000)]


def test_rpc_http_400_block_range_error_uses_existing_splitter():
    calls = []

    def query(method, params):
        start = int(params[0]["fromBlock"], 16)
        end = int(params[0]["toBlock"], 16)
        calls.append((start, end))
        if (start, end) == (1, 10_000):
            return {
                "error": {
                    "code": 35,
                    "message": "ranges over 5000 blocks are not supported on free plan",
                },
            }
        return {"result": []}

    client = client_with(query)
    assert client._logs_range("0x1", {}, 1, 10_000) == []
    assert calls == [(1, 10_000), (1, 5_000), (5_001, 10_000)]


def test_rpc_query_parses_json_rpc_error_from_http_400():
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": 35,
            "message": "ranges over 10000 blocks are not supported on free plan",
        },
    }

    class Store:
        def cached(self, source, params, suffix):
            return None

        def save(self, source, params, payload, suffix):
            pass

    class HttpClient:
        def post(self, url, json):
            return SimpleNamespace(
                content=b"response",
                text="response",
                status_code=400,
                raise_for_status=lambda: pytest.fail("accepted HTTP 400 should not be raised"),
                json=lambda: body,
            )

    client = RpcClient("https://rpc.invalid", 4663, Store(), SimpleNamespace(), 30, 0)
    client.client = HttpClient()

    assert client._query("eth_getLogs", [{}]) == body


def test_rpc_single_block_limit_fails_explicitly():
    client = client_with(lambda method, params: {
        "error": {"code": -32005, "message": "query returned more than 10000 results"},
    })
    with pytest.raises(ServiceError, match="one block"):
        client._logs_range("0x1", {}, 7, 7)


def test_rpc_uses_explorer_resolver_for_timestamp_ranges():
    calls = []
    resolver = SimpleNamespace(block_at=lambda timestamp, closest: calls.append((timestamp, closest)) or (
        10 if closest == "after" else 20
    ))
    client = client_with(lambda method, params: {"result": [make_log(12)]})
    client.block_resolver = resolver
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 2, tzinfo=timezone.utc)

    assert client.logs_full_range("0xABC", {}, start, end) == [make_log(12)]
    assert calls == [(int(start.timestamp()), "after"), (int(end.timestamp()) - 1, "before")]


def test_rpc_requests_start_at_least_point_one_seconds_apart():
    now = [10.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    class Store:
        def cached(self, source, params, suffix):
            return None

        def save(self, source, params, payload, suffix):
            pass

    class HttpClient:
        def post(self, url, json):
            return SimpleNamespace(
                content=b'{"jsonrpc":"2.0","result":[]}',
                text='{"jsonrpc":"2.0","result":[]}',
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {"jsonrpc": "2.0", "result": []},
            )

    pacer = RequestPacer(0.1, clock=lambda: now[0], sleeper=sleep)
    client = RpcClient("https://rpc.invalid", 4663, Store(), SimpleNamespace(), 30, 0, pacer)
    client.client = HttpClient()
    client._query("eth_getLogs", [{}])
    client._query("eth_getLogs", [{"address": "0x1"}])

    assert sleeps == [pytest.approx(0.1)]
