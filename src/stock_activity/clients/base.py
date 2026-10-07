from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Protocol

import httpx

from ..utils import hex_int, log_id


class ServiceError(RuntimeError):
    pass


class RequestPacer:
    def __init__(
        self,
        minimum_interval: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.minimum_interval = minimum_interval
        self.clock = clock
        self.sleeper = sleeper
        self.last_request_at: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self.last_request_at is not None:
            remaining = self.minimum_interval - (now - self.last_request_at)
            if remaining > 0:
                self.sleeper(remaining)
                now = self.clock()
        self.last_request_at = now


class HttpService:
    def __init__(self, timeout: float, retries: int):
        self.client = httpx.Client(timeout=timeout, follow_redirects=True)
        self.retries = retries

    def _before_request(self) -> None:
        pass

    def _request(
        self,
        method: str,
        url: str,
        *,
        accepted_statuses: set[int] | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                self._before_request()
                if method == "GET":
                    response = self.client.get(url, **kwargs)
                else:
                    response = self.client.post(url, **kwargs)
                if response.status_code == 429 or response.status_code >= 500:
                    raise ServiceError(f"transient HTTP {response.status_code}: {response.text[:200]}")
                if accepted_statuses and response.status_code in accepted_statuses:
                    return response
                response.raise_for_status()
                return response
            except (httpx.HTTPError, ServiceError) as exc:
                error = exc
                if attempt == self.retries:
                    break
                time.sleep(min(0.5 * 2**attempt, 8))
        raise ServiceError(f"request failed after {self.retries + 1} attempts: {error}")

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        return self._request("GET", url, params=params)

    def post(
        self, url: str, json: Any, *, accepted_statuses: set[int] | None = None,
    ) -> httpx.Response:
        return self._request("POST", url, json=json, accepted_statuses=accepted_statuses)


class BlockResolver(Protocol):
    def block_at(self, timestamp: int, closest: str) -> int: ...


class EventClient:
    LIMIT: int

    def __init__(self, chain_id: int, block_resolver: BlockResolver):
        self.chain_id = chain_id
        self.block_resolver = block_resolver

    def _logs_range(
        self, address: str, topics: dict[str, str], start: int, end: int,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    @staticmethod
    def _sort(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            logs,
            key=lambda row: (hex_int(row.get("blockNumber", 0)), hex_int(row.get("logIndex", 0))),
        )

    def logs(
        self, address: str, topics: dict[str, str], start: datetime, end: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch [start, end) in UTC-sized daily chunks, then stable-deduplicate."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("log boundaries must be timezone-aware")
        cursor = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        found: dict[str, dict[str, Any]] = {}
        resolver = getattr(self, "block_resolver", self)
        while cursor < end:
            boundary = min(cursor + timedelta(days=1), end)
            first = resolver.block_at(int(cursor.timestamp()), "after")
            last = resolver.block_at(int(boundary.timestamp()) - 1, "before")
            if first <= last:
                for item in self._logs_range(address.lower(), topics, first, last):
                    found[log_id(self.chain_id, item)] = item
            cursor = boundary
        return self._sort(list(found.values()))

    def logs_full_range(
        self, address: str, topics: dict[str, str], start: datetime, end: datetime,
    ) -> list[dict[str, Any]]:
        """Fetch [start, end) in one request, splitting only at the result cap."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("log boundaries must be timezone-aware")
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if start >= end:
            return []
        resolver = getattr(self, "block_resolver", self)
        first = resolver.block_at(int(start.timestamp()), "after")
        last = resolver.block_at(int(end.timestamp()) - 1, "before")
        if first > last:
            return []
        found = {
            log_id(self.chain_id, item): item
            for item in self._logs_range(address.lower(), topics, first, last)
        }
        return self._sort(list(found.values()))

    def logs_blocks(
        self, address: str, topics: dict[str, str], start: int, end: int,
    ) -> list[dict[str, Any]]:
        found = {
            log_id(self.chain_id, item): item
            for item in self._logs_range(address.lower(), topics, start, end)
        }
        return self._sort(list(found.values()))
