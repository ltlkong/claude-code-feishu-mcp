"""Per-chat notification debounce + batch pipeline.

One simple rule: hold a chat's incoming messages until ``debounce_seconds``
of silence, then flush everything pending as either a single notification
(when only one is buffered) or a ``batch``-typed notification (2+).

Earlier this used an adaptive short/long window — snappier for single
pings, longer once a burst started. Real-world usage (慈欣 typing in
spurts ~1-2s apart) hit the threshold edge constantly: messages that
should have batched flushed individually, and Claude answered each one
separately. Boss's call: collapse to a single 2s window. Simpler model,
predictable behavior, fewer edge cases.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Send a single JSON-RPC notification. Injected by the caller (mcp_server)
# because it needs the MCP write stream which is only available at runtime.
WriteFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class NotificationPipeline:
    """Buffer per-chat notifications until ``debounce_seconds`` of silence."""

    def __init__(
        self,
        write_fn: WriteFn,
        debounce_seconds: float = 2.0,
    ) -> None:
        self._write = write_fn
        self._debounce = float(debounce_seconds)

        self._buffers: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._flushing: set[str] = set()

    async def send(self, content: str, meta: dict[str, Any]) -> None:
        """Queue a notification for flushing.

        The notification is keyed by ``meta['chat_id']``; each chat has its
        own buffer so activity in one chat cannot delay notifications from
        another.
        """
        chat_id = meta.get("chat_id", "_unknown")
        self._buffers.setdefault(chat_id, []).append((content, meta))
        event = self._events.setdefault(chat_id, asyncio.Event())
        event.set()
        if chat_id not in self._flushing:
            self._flushing.add(chat_id)
            asyncio.create_task(self._flush(chat_id))

    async def _flush(self, chat_id: str) -> None:
        """Wait for silence, then flush the buffer.

        Each ``send()`` sets the per-chat asyncio Event, resetting the
        debounce window. After ``self._debounce`` seconds of no activity,
        flush whatever's buffered.
        """
        event = self._events.setdefault(chat_id, asyncio.Event())
        try:
            while True:
                event.clear()
                try:
                    await asyncio.wait_for(event.wait(), timeout=self._debounce)
                    # activity happened inside the window → restart debounce
                    continue
                except asyncio.TimeoutError:
                    # silence achieved → flush
                    pass
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
