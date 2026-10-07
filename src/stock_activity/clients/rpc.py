from __future__ import annotations

import json
import time
from typing import Any, Callable

from web3 import HTTPProvider

from ..rawstore import RawStore
from .base import BlockResolver, EventClient, HttpService, RequestPacer, ServiceError


class PacedHTTPProvider(HTTPProvider):
    """Apply the shared RPC pacer to Web3 calls as well as event requests."""

    def __init__(self, endpoint_uri: str, before_request: Callable[[], None], **kwargs: Any):
        super().__init__(endpoint_uri, **kwargs)
        self.before_request = before_request

    def make_request(self, method: str, params: Any) -> Any:
        self.before_request()
        return super().make_request(method, params)


class RpcClient(HttpService, EventClient):
    LIMIT = 10_000
    MAX_BLOCK_RANGE = 10_000
    REQUEST_INTERVAL = 0.1

    def __init__(
        self,
        url: str,
        chain_id: int,
        store: RawStore,
        block_resolver: BlockResolver,
        timeout: float,
        retries: int,
        pacer: RequestPacer | None = None,
    ):
        HttpService.__init__(self, timeout, retries)
        EventClient.__init__(self, chain_id, block_resolver)
        self.url = url
        self.store = store
        self.pacer = pacer or RequestPacer(self.REQUEST_INTERVAL)
        self._request_id = 0

    def _before_request(self) -> None:
        self.pacer.wait()

    @staticmethod
    def _limit_error(body: dict[str, Any]) -> bool:
        error = body.get("error")
        if not isinstance(error, dict):
            return False
        message = f"{error.get('message', '')} {error.get('data', '')}".lower()
        return error.get("code") == 35 or any(
            term in message
            for term in (
                "10,000", "10000", "limit", "too many", "response size", "block range", "ranges over",
            )
        )

    def _query(self, method: str, params: list[Any]) -> dict[str, Any]:
        cache_params = {"chain_id": self.chain_id, "method": method, "params": params}
        cached = self.store.cached("rpc", cache_params, "json")
        if cached is not None:
            return json.loads(cached)
        for attempt in range(self.retries + 1):
            self._request_id += 1
            request = {"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
            response = self.post(self.url, request, accepted_statuses={400})
            try:
                body = response.json()
            except ValueError as exc:
                self.store.save("rpc_attempt", {**cache_params, "attempt": attempt}, response.content, "json")
                if attempt < self.retries:
                    time.sleep(min(0.5 * 2**attempt, 8))
                    continue
                raise ServiceError(f"RPC endpoint returned non-JSON: {response.text[:200]}") from exc
            if not isinstance(body, dict):
                raise ServiceError(f"RPC endpoint returned a malformed response: {body!r}")
            if "result" in body:
                self.store.save("rpc", cache_params, response.content, "json")
                return body
            if self._limit_error(body):
                return body
            if attempt < self.retries:
                self.store.save("rpc_attempt", {**cache_params, "attempt": attempt}, response.content, "json")
                time.sleep(min(0.5 * 2**attempt, 8))
                continue
            raise ServiceError(f"RPC request failed: {body.get('error', body)}")
        raise ServiceError("unreachable RPC retry state")

    @staticmethod
    def _rpc_topics(topics: dict[str, str]) -> list[str | None]:
        indexed = {
            int(key.removeprefix("topic")): value
            for key, value in topics.items()
            if key.startswith("topic") and key[5:].isdigit()
        }
        if not indexed:
            return []
        return [indexed.get(index) for index in range(max(indexed) + 1)]

    def _logs_range(
        self, address: str, topics: dict[str, str], start: int, end: int,
    ) -> list[dict[str, Any]]:
        if end - start + 1 > self.MAX_BLOCK_RANGE:
            return self._split_logs_range(address, topics, start, end)
        log_filter = {
            "address": address.lower(),
            "fromBlock": hex(start),
            "toBlock": hex(end),
            "topics": self._rpc_topics(topics),
        }
        body = self._query("eth_getLogs", [log_filter])
        result = body.get("result", [])
        limit = self._limit_error(body) or (isinstance(result, list) and len(result) >= self.LIMIT)
        if limit:
            return self._split_logs_range(address, topics, start, end)
        if not isinstance(result, list):
            raise ServiceError(f"RPC eth_getLogs returned a malformed result: {body}")
        return result

    def _split_logs_range(
        self, address: str, topics: dict[str, str], start: int, end: int,
    ) -> list[dict[str, Any]]:
        if start == end:
            raise ServiceError(f"one block ({start}) reaches the RPC endpoint's {self.LIMIT}-log cap")
        middle = (start + end) // 2
        return (
            self._logs_range(address, topics, start, middle)
            + self._logs_range(address, topics, middle + 1, end)
        )
