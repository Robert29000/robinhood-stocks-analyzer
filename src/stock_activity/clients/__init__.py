from __future__ import annotations

# Export time for compatibility with callers that patch the former clients module's clock.
from . import blockscout as _blockscout
from .base import EventClient, HttpService, RequestPacer, ServiceError
from .blockscout import BlockscoutClient
from .rpc import PacedHTTPProvider, RpcClient

time = _blockscout.time

__all__ = [
    "BlockscoutClient",
    "EventClient",
    "HttpService",
    "PacedHTTPProvider",
    "RequestPacer",
    "RpcClient",
    "ServiceError",
]
