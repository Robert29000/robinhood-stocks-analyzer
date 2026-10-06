from __future__ import annotations

import pytest

from stock_activity.clients import BlockscoutClient, ServiceError


def client_with(query):
    client = object.__new__(BlockscoutClient)
    client.chain_id = 4663
    client._query = query
    return client


def make_log(block, index=0):
    return {"blockNumber": hex(block), "logIndex": hex(index), "transactionHash": "0x" + f"{block:064x}"}


def test_pro_api_query_includes_chainid_and_does_not_persist_key():
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

    client = object.__new__(BlockscoutClient)
    client.url = "https://api.blockscout.com/v2/api"
    client.api_key = "secret"
    client.chain_id = 4663
    client.store = Store()
    client.retries = 0
    client.get = lambda url, params: requests.append((url, params)) or Response()

    assert client._query({"module": "block", "action": "getblocknobytime"})["result"] == "123"
    assert requests[0][1]["chainid"] == 4663
    assert requests[0][1]["apikey"] == "secret"
    assert "apikey" not in saves[0][1]
    assert saves[0][1]["chainid"] == 4663


def test_v2_block_lookup_reads_nested_block_number():
    client = client_with(lambda params: {"status": "1", "message": "OK", "result": {"blockNumber": "653325"}})
    assert client.block_at(1782864000, "after") == 653325


def test_below_limit_does_not_split():
    calls = []
    client = client_with(lambda params: calls.append(params) or {"status": "1", "result": [make_log(1)]})
    assert len(client._logs_range("0x1", {}, 1, 2)) == 1
    assert len(calls) == 1


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
