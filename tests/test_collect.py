from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from stock_activity.clients import ServiceError
from stock_activity.collect import (
    ZERO_ADDRESS_TOPIC,
    RequestPacer,
    _dividend_scan_windows,
    _event_activity_windows,
    _fetch_alpha,
    _merge_windows,
    _mint_burn_logs,
    _swap_topics,
)
from stock_activity.contracts import ROBINHOOD_STOCK, UNISWAP_V3_POOL, UNISWAP_V4_POOL_MANAGER, event_topic


class Store:
    def __init__(self, cached=None):
        self.payload = cached
        self.saved = []

    def cached(self, source, params, suffix):
        return self.payload

    def save(self, source, params, payload, suffix):
        self.saved.append((source, params, payload, suffix))
        self.payload = payload


class Http:
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def get(self, url, params):
        self.requests.append((url, params))
        return SimpleNamespace(content=self.payload)


def settings():
    return SimpleNamespace(
        alpha_vantage_api_key="secret",
        alpha_vantage_url="https://example.invalid/query",
        ex_date_start=date(2026, 7, 1),
        ex_date_end=date(2026, 9, 30),
        retries=2,
    )


def test_alpha_dividends_request_and_cache_valid_csv():
    payload = (
        b"ex_dividend_date,declaration_date,record_date,payment_date,amount\n"
        b"2026-08-07,2026-07-01,2026-08-10,2026-08-14,0.25\n"
    )
    http, store = Http(payload), Store()
    rows = _fetch_alpha(settings(), http, store, "AAPL")
    assert rows == [{
        "ticker": "AAPL", "ex_dividend_date": "2026-08-07", "declaration_date": "2026-07-01",
        "record_date": "2026-08-10", "payment_date": "2026-08-14", "amount": "0.25",
    }]
    assert http.requests[0][1]["datatype"] == "csv"
    assert store.saved[0][1]["datatype"] == "csv"
    assert "apikey" not in store.saved[0][1]


def test_alpha_request_pacer_waits_between_requests():
    now = [100.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    pacer = RequestPacer(12, clock=lambda: now[0], sleeper=sleep)
    pacer.wait()
    now[0] += 2
    pacer.wait()

    assert sleeps == [10]


def test_alpha_request_pacing_is_skipped_for_cached_data():
    payload = b"ex_dividend_date,declaration_date,record_date,payment_date,amount\n"
    calls = []

    _fetch_alpha(settings(), Http(payload), Store(cached=payload), "AAPL", lambda: calls.append("wait"))

    assert calls == []


@pytest.mark.parametrize("payload", [
    b'{"Note":"API call frequency exceeded"}',
    b"temporarily unavailable",
])
def test_alpha_errors_are_not_cached(payload):
    http, store = Http(payload), Store()
    with pytest.raises(ServiceError):
        _fetch_alpha(settings(), http, store, "AAPL")
    assert len(http.requests) == 3
    assert store.saved == []


def test_alpha_empty_json_is_retried_and_can_recover():
    valid = (
        b"ex_dividend_date,declaration_date,record_date,payment_date,amount\n"
        b"2026-08-07,2026-07-01,2026-08-10,2026-08-14,0.25\n"
    )

    class SequenceHttp(Http):
        def __init__(self):
            super().__init__(None)
            self.payloads = iter([b"{}", valid])

        def get(self, url, params):
            self.requests.append((url, params))
            return SimpleNamespace(content=next(self.payloads))

    http, store, paced = SequenceHttp(), Store(), []
    rows = _fetch_alpha(settings(), http, store, "AAPL", lambda: paced.append(True))

    assert len(http.requests) == 2
    assert len(paced) == 2
    assert rows[0]["ticker"] == "AAPL"
    assert store.payload == valid


def test_mint_and_burn_logs_use_separate_indexed_address_filters_and_deduplicate():
    start = object()
    end = object()
    mint = {"blockNumber": "0x1", "logIndex": "0x0", "transactionHash": "0xmint"}
    zero_to_zero = {"blockNumber": "0x2", "logIndex": "0x0", "transactionHash": "0xboth"}
    burn = {"blockNumber": "0x3", "logIndex": "0x0", "transactionHash": "0xburn"}

    class Blockscout:
        def __init__(self):
            self.calls = []

        def logs(self, address, topics, range_start, range_end):
            self.calls.append((address, topics, range_start, range_end))
            return [mint, zero_to_zero] if "topic1" in topics else [zero_to_zero, burn]

    blockscout = Blockscout()
    logs = _mint_burn_logs(blockscout, 4663, "0xtoken", start, end)

    transfer_topic = event_topic(ROBINHOOD_STOCK.events.Transfer)
    assert [call[1] for call in blockscout.calls] == [
        {"topic0": transfer_topic, "topic1": ZERO_ADDRESS_TOPIC},
        {"topic0": transfer_topic, "topic2": ZERO_ADDRESS_TOPIC},
    ]
    assert all(call[0] == "0xtoken" and call[2:] == (start, end) for call in blockscout.calls)
    assert logs == [mint, zero_to_zero, burn]


def test_v4_swap_topics_filter_pool_manager_logs_by_pool_id():
    pool_id = "0x" + "ab" * 32

    assert _swap_topics("v4", pool_id) == {
        "topic0": event_topic(UNISWAP_V4_POOL_MANAGER.events.Swap),
        "topic1": pool_id,
    }
    assert _swap_topics("v3", "0xpool") == {
        "topic0": event_topic(UNISWAP_V3_POOL.events.Swap),
    }


def test_dividend_scan_windows_are_per_event_and_merged_per_ticker():
    dividends = [
        {"ticker": "AAPL", "ex_dividend_date": "2026-08-10", "payment_date": "2026-08-13"},
        {"ticker": "AAPL", "ex_dividend_date": "2026-08-15", "payment_date": "2026-08-20"},
        {"ticker": "MSFT", "ex_dividend_date": "2026-08-20", "payment_date": "2026-09-10"},
    ]
    cutoff = datetime(2026, 10, 1, tzinfo=timezone.utc)

    rows, queries = _dividend_scan_windows(dividends, 2, cutoff)

    assert len(rows) == 3
    assert rows[0]["start"] == "2026-08-08T00:00:00+00:00"
    assert rows[0]["end"] == "2026-08-16T00:00:00+00:00"
    assert queries["AAPL"] == [(
        datetime(2026, 8, 8, tzinfo=timezone.utc),
        datetime(2026, 8, 23, tzinfo=timezone.utc),
    )]
    assert queries["MSFT"] == [(
        datetime(2026, 8, 18, tzinfo=timezone.utc),
        datetime(2026, 9, 13, tzinfo=timezone.utc),
    )]


def test_event_windows_keep_fixed_activity_and_symmetric_transition_ranges():
    emission = int(datetime(2026, 8, 8, 12, tzinfo=timezone.utc).timestamp())
    effective = int(datetime(2026, 8, 10, 12, tzinfo=timezone.utc).timestamp())
    updates = [{
        "id": "update-1", "ticker": "AAPL",
        "emission_timestamp": emission, "effective_timestamp": effective,
    }]
    cutoff = datetime(2026, 9, 1, tzinfo=timezone.utc)

    activity, transitions, queries = _event_activity_windows(updates, 1, 3, cutoff)

    assert activity == [{
        "update_id": "update-1", "ticker": "AAPL",
        "start": "2026-08-09T12:00:00+00:00", "end": "2026-08-13T12:00:00+00:00",
    }]
    assert transitions == [{
        "update_id": "update-1", "ticker": "AAPL",
        "start": "2026-08-08T12:00:00+00:00", "end": "2026-08-12T12:00:00+00:00",
    }]
    assert queries["AAPL"] == [(
        datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
        datetime(2026, 8, 13, 12, 0, 1, tzinfo=timezone.utc),
    )]


def test_merge_windows_discards_empty_ranges():
    point = datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert _merge_windows([(point, point)]) == []
