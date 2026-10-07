from __future__ import annotations

import json
import time
from typing import Any

from ..rawstore import RawStore
from ..utils import hex_int
from .base import EventClient, HttpService, ServiceError


class ExplorerClient(HttpService, EventClient):
    """Client for Etherscan-compatible explorer APIs, including Blockscout."""

    DEFAULT_LOG_LIMIT = 1_000
    DEFAULT_REQUEST_INTERVAL = 0.5

    def __init__(
        self,
        url: str,
        api_key: str | None,
        chain_id: int,
        store: RawStore,
        timeout: float,
        retries: int,
        request_interval: float = DEFAULT_REQUEST_INTERVAL,
        log_limit: int = DEFAULT_LOG_LIMIT,
    ):
        HttpService.__init__(self, timeout, retries)
        EventClient.__init__(self, chain_id, self)
        self.url = url
        self.api_key = api_key
        self.store = store
        self.request_interval = request_interval
        self.log_limit = log_limit
        self._last_request_at: float | None = None

    def _before_request(self) -> None:
        now = time.monotonic()
        if self._last_request_at is not None:
            remaining = self.request_interval - (now - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
                now = time.monotonic()
        self._last_request_at = now

    def _query(self, params: dict[str, Any]) -> dict[str, Any]:
        request_params = dict(params)
        request_params["chainid"] = self.chain_id
        if self.api_key:
            request_params["apikey"] = self.api_key
        cache_params = {"endpoint": self.url, **{k: v for k, v in request_params.items() if k != "apikey"}}
        cached = self.store.cached("explorer", cache_params, "json")
        if cached is not None:
            return json.loads(cached)
        for attempt in range(self.retries + 1):
            response = self.get(self.url, request_params)
            try:
                body = response.json()
            except ValueError as exc:
                self.store.save("explorer_attempt", {**cache_params, "attempt": attempt}, response.content, "json")
                if attempt < self.retries:
                    time.sleep(min(0.5 * 2**attempt, 8))
                    continue
                raise ServiceError(f"explorer returned non-JSON: {response.text[:200]}") from exc
            message = f"{body.get('message', '')} {body.get('result', '')}".lower()
            transient = any(
                term in message
                for term in ("rate limit", "temporarily unavailable", "timeout", "try again")
            )
            if transient and attempt < self.retries:
                self.store.save("explorer_attempt", {**cache_params, "attempt": attempt}, response.content, "json")
                time.sleep(min(0.5 * 2**attempt, 8))
                continue
            self.store.save("explorer", cache_params, response.content, "json")
            return body
        raise ServiceError("unreachable explorer retry state")

    def block_at(self, timestamp: int, closest: str) -> int:
        body = self._query({
            "module": "block", "action": "getblocknobytime", "timestamp": timestamp, "closest": closest,
        })
        if str(body.get("status")) != "1":
            raise ServiceError(f"explorer block lookup failed: {body}")
        result = body["result"]
        if isinstance(result, dict):
            result = result.get("blockNumber")
        if result is None:
            raise ServiceError(f"explorer block lookup omitted blockNumber: {body}")
        return hex_int(result)

    def _limit_error(self, body: dict[str, Any]) -> bool:
        result = body.get("result", "")
        message = f"{body.get('message', '')} {result if isinstance(result, str) else ''}".lower()
        rendered_limit = f"{self.log_limit:,}"
        return any(
            term in message
            for term in (
                rendered_limit,
                str(self.log_limit),
                "too many results",
                "result window",
                "response size",
            )
        )

    def _logs_range(
        self, address: str, topics: dict[str, str], start: int, end: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "module": "logs",
            "action": "getLogs",
            "address": address,
            "fromBlock": start,
            "toBlock": end,
            "page": 1,
            "offset": self.log_limit,
        }
        params.update(topics)
        indexes = sorted(
            int(key.removeprefix("topic"))
            for key in topics
            if key.startswith("topic") and key[5:].isdigit()
        )
        for left, right in zip(indexes, indexes[1:]):
            params[f"topic{left}_{right}_opr"] = "and"
        body = self._query(params)
        result = body.get("result", [])
        limit = self._limit_error(body) or (isinstance(result, list) and len(result) >= self.log_limit)
        if limit:
            if start == end:
                raise ServiceError(f"one block ({start}) reaches the explorer's {self.log_limit}-log cap")
            middle = (start + end) // 2
            return (
                self._logs_range(address, topics, start, middle)
                + self._logs_range(address, topics, middle + 1, end)
            )
        if str(body.get("status")) == "0":
            message = str(body.get("message", "")) + str(result)
            if "no records" in message.lower() or result == []:
                return []
            raise ServiceError(f"explorer logs request failed: {body}")
        if not isinstance(result, list):
            raise ServiceError(f"explorer returned a malformed logs response: {body}")
        return result
