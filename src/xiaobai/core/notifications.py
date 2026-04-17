"""Per-chat notification debounce + batch pipeline.

Ported verbatim from ``feishu_channel/server.py`` (lines 648–714) so that
Session 2's ``mcp_server`` can plug it in unchanged. Behavior is identical to
the original; only the surrounding class changed.

HOLDS: the live server.py still has this code inline — it is NOT removed in
Session 1. Session 2 will switch server.py to this module and delete the
inline copy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Send a single JSON-RPC notification. Injected by the caller (mcp_server)
# because it needs the MCP write stream which is only available at runtime.
WriteFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class NotificationPipeline:
    """Buffer per-chat notifications; flush after 3s of silence.

    Text messages are debounced: buffered for up to 3 seconds, then all
    pending notifications for the same chat are flushed. A single pending
    notification is sent as-is; multiple notifications are merged into one
    ``batch``-typed notification whose ``content`` is a JSON array of the
    originals. Non-text messages (heartbeat, card actions, media) go through
    the same pipeline — the logic here matches the old ``server.py`` byte-
    for-byte so existing downstream consumers still see the same payloads.
    """

    def __init__(self, write_fn: WriteFn, debounce_seconds: float = 3.0) -> None:
        self._write = write_fn
        self._debounce = debounce_seconds

        self._buffers: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self._last_appends: dict[str, float] = {}
        self._flushing: set[str] = set()

    async def send(self, content: str, meta: dict[str, Any]) -> None:
        """Queue a notification for flushing.

        The notification is keyed by ``meta['chat_id']``; each chat has its
        own buffer so activity in one chat cannot delay notifications from
        another.
        """
        chat_id = meta.get("chat_id", "_unknown")
        self._buffers.setdefault(chat_id, []).append((content, meta))
        self._last_appends[chat_id] = time.time()
        if chat_id not in self._flushing:
            self._flushing.add(chat_id)
            asyncio.create_task(self._flush(chat_id))

    async def _flush(self, chat_id: str) -> None:
        """Poll until ``debounce`` seconds of silence, then flush the buffer."""
        try:
            while True:
                while True:
                    await asyncio.sleep(0.5)
                    if time.time() - self._last_appends.get(chat_id, 0) >= self._debounce:
                        break
                pending = self._buffers.get(chat_id, [])[:]
                self._buffers[chat_id] = []
                if len(pending) == 1:
                    await self._write(pending[0][0], pending[0][1])
                elif len(pending) > 1:
                    msgs = []
                    for c, m in pending:
                        msgs.append({
                            "user_id": m.get("user_id", ""),
                            "message_time": m.get("message_time", ""),
                            "message_id": m.get("message_id", ""),
                            "request_id": m.get("request_id", ""),
                            "content": c,
                        })
                    merged_content = json.dumps(msgs, ensure_ascii=False)
                    merged_meta = dict(pending[-1][1])
                    merged_meta["message_type"] = "batch"
                    await self._write(merged_content, merged_meta)
                if not self._buffers.get(chat_id):
                    break
        finally:
            self._flushing.discard(chat_id)
