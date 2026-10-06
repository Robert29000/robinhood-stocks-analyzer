from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .utils import hex_int, log_id
from .rawstore import RawStore


class ServiceError(RuntimeError):
    pass


class HttpService:
    def __init__(self, timeout: float, retries: int):
        self.client = httpx.Client(timeout=timeout, follow_redirects=True)
        self.retries = retries

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.client.get(url, params=params)
                if response.status_code == 429 or response.status_code >= 500:
                    raise ServiceError(f"transient HTTP {response.status_code}: {response.text[:200]}")
                response.raise_for_status()
                return response
            except (httpx.HTTPError, ServiceError) as exc:
                error = exc
                if attempt == self.retries:
                    break
                time.sleep(min(0.5 * 2**attempt, 8))
        raise ServiceError(f"request failed after {self.retries + 1} attempts: {error}")


class BlockscoutClient(HttpService):
    LIMIT = 1_000

    def __init__(self, url: str, api_key: str | None, chain_id: int, store: RawStore, timeout: float, retries: int):
        super().__init__(timeout, retries)
        self.url = url
        self.api_key = api_key
        self.chain_id = chain_id
        self.store = store

    def _query(self, params: dict[str, Any]) -> dict[str, Any]:
        request_params = dict(params)
        request_params["chainid"] = self.chain_id
        if self.api_key:
            request_params["apikey"] = self.api_key
        cache_params = {"endpoint": self.url, **{k: v for k, v in request_params.items() if k != "apikey"}}
        cached = self.store.cached("blockscout", cache_params, "json")
        if cached is not None:
            return json.loads(cached)
        for attempt in range(self.retries + 1):
            response = self.get(self.url, request_params)
            try:
                body = response.json()
            except ValueError as exc:
                self.store.save("blockscout_attempt", {**cache_params, "attempt": attempt}, response.content, "json")
                if attempt < self.retries:
                    time.sleep(min(0.5 * 2**attempt, 8))
                    continue
                raise ServiceError(f"Blockscout returned non-JSON: {response.text[:200]}") from exc
            message = f"{body.get('message', '')} {body.get('result', '')}".lower()
            transient = any(term in message for term in ("rate limit", "temporarily unavailable", "timeout", "try again"))
            if transient and attempt < self.retries:
                self.store.save("blockscout_attempt", {**cache_params, "attempt": attempt}, response.content, "json")
                time.sleep(min(0.5 * 2**attempt, 8))
                continue
            self.store.save("blockscout", cache_params, response.content, "json")
            return body
        raise ServiceError("unreachable Blockscout retry state")

    def block_at(self, timestamp: int, closest: str) -> int:
        body = self._query({
            "module": "block", "action": "getblocknobytime", "timestamp": timestamp, "closest": closest
        })
        if str(body.get("status")) != "1":
            raise ServiceError(f"Blockscout block lookup failed: {body}")
        result = body["result"]
        if isinstance(result, dict):
            result = result.get("blockNumber")
        if result is None:
            raise ServiceError(f"Blockscout block lookup omitted blockNumber: {body}")
        return hex_int(result)

    @staticmethod
    def _limit_error(body: dict[str, Any]) -> bool:
        result = body.get("result", "")
        message = f"{body.get('message', '')} {result if isinstance(result, str) else ''}".lower()
        return any(term in message for term in ("1,000", "1000", "10,000", "10000", "limit", "too many"))

    def _logs_range(self, address: str, topics: dict[str, str], start: int, end: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "module": "logs", "action": "getLogs", "address": address,
            "fromBlock": start, "toBlock": end,
        }
        params.update(topics)
        indexes = sorted(int(key.removeprefix("topic")) for key in topics if key.startswith("topic") and key[5:].isdigit())
        for left, right in zip(indexes, indexes[1:]):
            params[f"topic{left}_{right}_opr"] = "and"
        body = self._query(params)
        result = body.get("result", [])
        limit = self._limit_error(body) or (isinstance(result, list) and len(result) >= self.LIMIT)
        if limit:
            if start == end:
                raise ServiceError(f"one block ({start}) reaches Blockscout's {self.LIMIT}-log cap")
            middle = (start + end) // 2
            return self._logs_range(address, topics, start, middle) + self._logs_range(address, topics, middle + 1, end)
        if str(body.get("status")) == "0":
            message = str(body.get("message", "")) + str(result)
            if "no records" in message.lower() or result == []:
                return []
            raise ServiceError(f"Blockscout logs request failed: {body}")
        if not isinstance(result, list):
            raise ServiceError(f"Blockscout malformed logs response: {body}")
        return result

    def logs(self, address: str, topics: dict[str, str], start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Fetch [start, end) in UTC-sized daily chunks, then stable-deduplicate."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("log boundaries must be timezone-aware")
        cursor = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        found: dict[str, dict[str, Any]] = {}
        while cursor < end:
            boundary = min(cursor + timedelta(days=1), end)
            first = self.block_at(int(cursor.timestamp()), "after")
            last = self.block_at(int(boundary.timestamp()) - 1, "before")
            if first <= last:
                for item in self._logs_range(address.lower(), topics, first, last):
                    found[log_id(self.chain_id, item)] = item
            cursor = boundary
        return sorted(found.values(), key=lambda row: (hex_int(row.get("blockNumber", 0)), hex_int(row.get("logIndex", 0))))

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
        first = self.block_at(int(start.timestamp()), "after")
        last = self.block_at(int(end.timestamp()) - 1, "before")
        if first > last:
            return []
        found = {
            log_id(self.chain_id, item): item
            for item in self._logs_range(address.lower(), topics, first, last)
        }
        return sorted(
            found.values(),
            key=lambda row: (hex_int(row.get("blockNumber", 0)), hex_int(row.get("logIndex", 0))),
        )

    def logs_blocks(self, address: str, topics: dict[str, str], start: int, end: int) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for item in self._logs_range(address, topics, start, end):
            found[log_id(self.chain_id, item)] = item
        return sorted(found.values(), key=lambda row: (hex_int(row.get("blockNumber", 0)), hex_int(row.get("logIndex", 0))))
