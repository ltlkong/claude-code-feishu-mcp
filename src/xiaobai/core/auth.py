"""Generic token provider with single-flight refresh.

Consolidates the duplicate ``_get_token`` logic found in the legacy
``feishu_channel/card.py`` (CardManager._get_token, lines 102–110) and
``feishu_channel/feishu.py`` (FeishuListener._get_token, lines 382–391).

Usage::

    provider = TokenProvider(
        name="feishu",
        fetch=lambda http: fetch_tenant_token(http, app_id, app_secret),
        ttl_seconds=5400,
        http=self._http,
    )
    token = await provider.get()
    ...
    provider.invalidate()  # force refresh on next .get()

Single-flight: concurrent callers share a single in-flight refresh via
``asyncio.Lock``. The cached token is considered valid until ``ttl_seconds``
has passed since it was fetched.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Generic, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")

FetchFn = Callable[[httpx.AsyncClient], Awaitable[T]]


class TokenProvider(Generic[T]):
    """Cache + refresh a token-like value fetched via ``fetch(http)``.

    Thread-safety: not thread-safe; use from a single asyncio loop.
    """

    def __init__(
        self,
        name: str,
        fetch: FetchFn[T],
        http: httpx.AsyncClient,
        ttl_seconds: float = 5400.0,
    ) -> None:
        self._name = name
        self._fetch = fetch
        self._http = http
        self._ttl = ttl_seconds
        self._value: T | None = None
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> T:
        """Return the cached token, refreshing if it has expired."""
        if self._value is not None and (time.time() - self._fetched_at) < self._ttl:
            return self._value
        async with self._lock:
            # Double-check after acquiring the lock — someone else may have
            # refreshed while we were waiting.
            if self._value is not None and (time.time() - self._fetched_at) < self._ttl:
                return self._value
            from ..utils.logging import span
            async with span("token.fetch", name=self._name):
                self._value = await self._fetch(self._http)
            self._fetched_at = time.time()
            logger.info("TokenProvider[%s] refreshed", self._name)
            return self._value

    def invalidate(self) -> None:
        """Force a refresh on the next ``get()`` call."""
        self._value = None
        self._fetched_at = 0.0
