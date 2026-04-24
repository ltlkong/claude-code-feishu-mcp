"""Generic token provider with single-flight refresh + failure circuit breaker.

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

Circuit breaker: when a fetch fails, the provider records the failure time.
Subsequent ``get()`` calls within ``failure_cooldown_seconds`` raise a
``TokenFetchUnavailable`` exception immediately, instead of re-hammering
the (already broken) auth endpoint. This prevents 21 concurrent tool call
sites from each driving 2 fetch attempts during an auth outage. The first
caller after the cooldown attempts a real fetch again.
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


class TokenFetchUnavailable(RuntimeError):
    """Raised when the circuit breaker is open due to recent fetch failures."""


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
        failure_cooldown_seconds: float = 5.0,
    ) -> None:
        self._name = name
        self._fetch = fetch
        self._http = http
        self._ttl = ttl_seconds
        self._cooldown = failure_cooldown_seconds
        self._value: T | None = None
        self._fetched_at: float = 0.0
        self._last_failure: float = 0.0
        self._consecutive_failures: int = 0
        self._lock = asyncio.Lock()

    async def get(self) -> T:
        """Return the cached token, refreshing if it has expired.

        Raises :class:`TokenFetchUnavailable` if the circuit breaker is open
        from recent consecutive fetch failures.
        """
        if self._value is not None and (time.time() - self._fetched_at) < self._ttl:
            return self._value
        # Circuit breaker — fail fast if we recently failed.
        if self._last_failure and (time.time() - self._last_failure) < self._cooldown:
            raise TokenFetchUnavailable(
                f"TokenProvider[{self._name}] circuit open: "
                f"{self._consecutive_failures} consecutive failures, "
                f"retry after {self._cooldown:.0f}s cooldown"
            )
        async with self._lock:
            # Double-check after acquiring the lock — someone else may have
            # refreshed while we were waiting.
            if self._value is not None and (time.time() - self._fetched_at) < self._ttl:
                return self._value
            # Re-check the circuit under the lock: another coroutine that
            # entered the lock first may have failed and tripped it.
            if self._last_failure and (time.time() - self._last_failure) < self._cooldown:
                raise TokenFetchUnavailable(
                    f"TokenProvider[{self._name}] circuit open during lock"
                )
            from ..utils.logging import span
            try:
                async with span("token.fetch", token_name=self._name):
                    self._value = await self._fetch(self._http)
            except Exception:
                self._last_failure = time.time()
                self._consecutive_failures += 1
                logger.warning(
                    "TokenProvider[%s] fetch failed (%d consecutive)",
                    self._name, self._consecutive_failures,
                )
                raise
            self._fetched_at = time.time()
            self._consecutive_failures = 0
            self._last_failure = 0.0
            logger.info("TokenProvider[%s] refreshed", self._name)
            return self._value

    def invalidate(self) -> None:
        """Force a refresh on the next ``get()`` call."""
        self._value = None
        self._fetched_at = 0.0
