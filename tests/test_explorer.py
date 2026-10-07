from __future__ import annotations

from datetime import datetime, timezone

import pytest

import stock_activity.clients as clients_module
from stock_activity.clients import ExplorerClient, ServiceError


def client_with(query):
    client = object.__new__(ExplorerClient)
    client.chain_id = 4663
    client.log_limit = 1_000
    client._query = query
    return client


def make_log(block, index=0):
    return {"blockNumber": hex(block), "logIndex": hex(index), "transactionHash": "0x" + f"{block:064x}"}


def test_explorer_query_includes_chainid_and_does_not_persist_key():
    requests, saves = [], []

    class Store:
        def cached(self, source, params, suffix):
            return None

        def save(self, source, params, payload, suffix):
            saves.append((source, params))

    class Response:
        content = b'{"status":"1","result":"123"}'

        def json(self):
            return {"status": "1", "result": "123"}

    client = object.__new__(ExplorerClient)
    client.url = "https://api.example.invalid/v2/api"
    client.api_key = "secret"
    client.chain_id = 4663
    client.store = Store()
    client.retries = 0
    client.get = lambda url, params: requests.append((url, params)) or Response()

    assert client._query({"module": "block", "action": "getblocknobytime"})["result"] == "123"
    assert requests[0][1]["chainid"] == 4663
    assert requests[0][1]["apikey"] == "secret"
    assert saves[0][0] == "explorer"
    assert "apikey" not in saves[0][1]
    assert saves[0][1]["chainid"] == 4663


def test_explorer_uses_configured_request_interval(monkeypatch):
    now = [10.0]
    sleeps = []

    class Store:
        def cached(self, source, params, suffix):
            return None

        def save(self, source, params, payload, suffix):
            pass

    class Response:
        status_code = 200
        content = b'{"status":"1","result":"123"}'
        text = content.decode()

        def raise_for_status(self):
            pass

        def json(self):
            return {"status": "1", "result": "123"}

    class HttpClient:
        def __init__(self):
            self.calls = []

        def get(self, url, params):
            self.calls.append((now[0], url, params))
            return Response()

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(clients_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(clients_module.time, "sleep", sleep)
    client = ExplorerClient(
        "https://example.invalid", "secret", 4663, Store(), 30, 0, request_interval=0.5,
    )
    client.client = HttpClient()

    client._query({"module": "block", "action": "first"})
    client._query({"module": "block", "action": "second"})

    assert sleeps == [pytest.approx(0.5)]
    assert [call[0] for call in client.client.calls] == [10.0, pytest.approx(10.5)]


def test_explorer_error_backoff_is_not_stacked_with_request_interval(monkeypatch):
    now = [10.0]
    sleeps = []

    class Store:
        pass

    class Response:
        text = "response"

        def __init__(self, status_code):
            self.status_code = status_code

        def raise_for_status(self):
            pass

    class HttpClient:
        def __init__(self):
            self.responses = iter((Response(500), Response(200)))

        def get(self, url, params):
            return next(self.responses)

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(clients_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(clients_module.time, "sleep", sleep)
    client = ExplorerClient("https://example.invalid", "secret", 4663, Store(), 30, 1)
    client.client = HttpClient()

    assert client.get(client.url).status_code == 200
    assert sleeps == [pytest.approx(0.5)]


def test_v2_block_lookup_reads_nested_block_number():
    client = client_with(lambda params: {"status": "1", "message": "OK", "result": {"blockNumber": "653325"}})
    assert client.block_at(1782864000, "after") == 653325


def test_below_limit_does_not_split():
    calls = []
    client = client_with(lambda params: calls.append(params) or {"status": "1", "result": [make_log(1)]})
    assert len(client._logs_range("0x1", {}, 1, 2)) == 1
    assert len(calls) == 1
    assert calls[0]["page"] == 1
    assert calls[0]["offset"] == 1_000


def test_limit_text_inside_log_payload_does_not_split():
    calls = []
    log = {**make_log(1), "data": "0x1000"}
    client = client_with(lambda params: calls.append(params) or {"status": "1", "message": "OK", "result": [log]})
    assert client._logs_range("0x1", {}, 1, 1) == [log]
    assert len(calls) == 1


def test_exact_limit_recursively_splits():
    calls = []
    def query(params):
        calls.append((params["fromBlock"], params["toBlock"]))
        if params["fromBlock"] == 1 and params["toBlock"] == 2:
            return {"status": "1", "result": [make_log(1, i) for i in range(1_000)]}
        return {"status": "1", "result": [make_log(params["fromBlock"])]}
    client = client_with(query)
    assert len(client._logs_range("0x1", {}, 1, 2)) == 2
    assert calls == [(1, 2), (1, 1), (2, 2)]


def test_single_block_limit_fails_explicitly():
    client = client_with(lambda params: {"status": "0", "message": "Query returned more than 1000 results", "result": []})
    with pytest.raises(ServiceError, match="one block"):
        client._logs_range("0x1", {}, 7, 7)


def test_full_range_logs_use_one_block_range_request():
    client = object.__new__(ExplorerClient)
    client.chain_id = 4663
    block_calls = []
    range_calls = []
    client.block_at = lambda timestamp, closest: block_calls.append((timestamp, closest)) or (
        10 if closest == "after" else 20
    )
    client._logs_range = lambda address, topics, start, end: (
        range_calls.append((address, topics, start, end)) or [make_log(12)]
    )
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = datetime(2026, 8, 20, tzinfo=timezone.utc)

    assert client.logs_full_range("0xABC", {"topic0": "0xtopic"}, start, end) == [make_log(12)]
    assert block_calls == [
        (int(start.timestamp()), "after"),
        (int(end.timestamp()) - 1, "before"),
    ]
    assert range_calls == [("0xabc", {"topic0": "0xtopic"}, 10, 20)]
