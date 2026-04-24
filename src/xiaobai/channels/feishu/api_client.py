"""Thin HTTP wrapper that centralizes Feishu's token-refresh retry pattern.

Every Feishu tool handler previously embedded the same 8-line boilerplate::

    for attempt in range(2):
        try:
            token = await token_provider.get()
            resp = await http.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
            data = resp.json()
            if attempt == 0 and is_token_error(data):
                token_provider.invalidate()
                continue
            if data.get("code") != 0:
                return {"status": "error", ...}
            ...

This was copy-pasted 21 times across tools/docs.py, channels/feishu/channel.py,
channels/feishu/cards.py, and tools/profile.py. An upgrade to Feishu's auth API
meant touching 21 places; a bug lived independently in each.

:class:`FeishuApiClient` pulls out the retry + token-error detection. Callers
do their own domain-level error handling (``data.get("code") != 0``) and
network-error catching. The method returns the parsed JSON body regardless
of its ``code`` — *only* HTTP / JSON decode errors propagate.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .api import is_token_error
from ...core.auth import TokenProvider

logger = logging.getLogger(__name__)


class FeishuApiClient:
    """Wrap ``httpx`` + ``TokenProvider`` with the retry-on-token-error loop.

    Bind this to a ``FeishuChannel`` once at startup; tool handlers can then
    call ``channel.api.request_json(...)`` instead of re-implementing the loop.
    """

    def __init__(self, http: httpx.AsyncClient, token_provider: TokenProvider[str]) -> None:
        self._http = http
        self._token = token_provider

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        data: Any = None,
        files: Any = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Call the Feishu API with a single token refresh on 99991663 / 99991664.

        Returns the parsed JSON body. Raises on network-level exceptions so
        callers keep their own try/except. Unlike the legacy call sites, the
        token-refresh retry ceiling is enforced here (max two attempts).
        """
        last_data: dict[str, Any] = {}
        for attempt in range(2):
            token = await self._token.get()
            merged_headers = dict(headers or {})
            merged_headers["Authorization"] = f"Bearer {token}"
            resp = await self._http.request(
                method,
                url,
                headers=merged_headers,
                json=json_body,
                data=data,
                files=files,
                params=params,
            )
            last_data = resp.json()
            if attempt == 0 and is_token_error(last_data):
                logger.info(
                    "FeishuApiClient[%s]: token expired, refreshing and retrying",
                    url.rsplit("/", 1)[-1],
                )
                self._token.invalidate()
                continue
            return last_data
        return last_data

    async def post_json(self, url: str, json_body: Any, **kwargs: Any) -> dict[str, Any]:
        """Convenience for the common POST + JSON body case."""
        return await self.request_json("POST", url, json_body=json_body, **kwargs)

    async def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
        """Convenience for GET with query params."""
        return await self.request_json("GET", url, params=params, **kwargs)
