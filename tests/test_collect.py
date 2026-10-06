from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from stock_activity.clients import ServiceError
from stock_activity.collect import RequestPacer, _fetch_alpha


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
    assert store.saved == []
