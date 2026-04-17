"""Small HTTP helpers shared by Feishu listener / cards / channel.

These are thin wrappers over the Feishu Open Platform REST endpoints. The
legacy code inlined the same calls in several places (card.py,
feishu.py::_get_token, server.py::_handle_read_messages, server.py media
downloads). We consolidate them here without changing behavior so both the
new ``FeishuChannel`` and the ported listener/cards can share them.
"""

from __future__ import annotations

import httpx


async def fetch_tenant_token(
    http: httpx.AsyncClient, app_id: str, app_secret: str
) -> str:
    """Exchange app_id/app_secret for a tenant_access_token (valid ~2h).

    Raises RuntimeError on non-zero ``code`` in the response body.
    """
    resp = await http.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get tenant token: {data}")
    return data["tenant_access_token"]


def is_token_error(data: dict) -> bool:
    """True if a Feishu API response indicates an expired / invalid token."""
    return data.get("code") in (99991663, 99991664, 99991668)


async def pull_recent_messages(
    http: httpx.AsyncClient, token: str, chat_id: str, count: int = 5
) -> list[dict]:
    """Pull up to ``count`` recent messages from a Feishu chat via REST.

    Returns an empty list on error (caller logs the reason). Matches the
    legacy behavior of ``FeishuListener._pull_recent_messages``.
    """
    import logging
    logger = logging.getLogger(__name__)
    try:
        resp = await http.get(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={
                "container_id_type": "chat",
                "container_id": chat_id,
                "sort_type": "ByCreateTimeDesc",
                "page_size": count,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        data = resp.json()
        if data.get("code") == 0:
            return data.get("data", {}).get("items", [])
    except Exception as e:
        logger.debug("Pull messages failed for %s: %s", chat_id, e)
    return []
