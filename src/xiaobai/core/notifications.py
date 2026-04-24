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
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Send a single JSON-RPC notification. Injected by the caller (mcp_server)
# because it needs the MCP write stream which is only available at runtime.
WriteFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class NotificationPipeline:
    """Buffer per-chat notifications with adaptive debounce.

    Text messages are debounced: buffered until silence, then all pending
    notifications for the same chat are flushed. A single pending
    notification is sent as-is; multiple notifications are merged into one
    ``batch``-typed notification whose ``content`` is a JSON array of the
    originals.

    The debounce window is adaptive:

    - ``short_debounce`` (default 1.0s) applies while the buffer holds a
      single notification — a lone message gets flushed fast for snappy
      responsiveness on sparse chats.
    - ``long_debounce`` (default 3.0s) applies once the buffer has 2+
      pending notifications — a burst in progress waits longer so related
      messages end up in the same batch.

    Legacy single-``debounce_seconds`` constructor still works; it binds
    both windows to the same value and keeps the old behavior.
    """

    def __init__(
        self,
        write_fn: WriteFn,
        debounce_seconds: float | None = None,
        *,
        short_debounce_seconds: float = 1.0,
        long_debounce_seconds: float = 3.0,
    ) -> None:
        self._write = write_fn
        if debounce_seconds is not None:
            # Legacy single-knob mode — both windows collapse to the value.
            self._short_debounce = float(debounce_seconds)
            self._long_debounce = float(debounce_seconds)
        else:
            self._short_debounce = float(short_debounce_seconds)
            self._long_debounce = float(long_debounce_seconds)

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

    def _current_debounce(self, chat_id: str) -> float:
        """Pick the debounce window based on buffer depth.

        Single pending message → snappy flush (``short_debounce``).
        Burst in progress (2+) → wait longer to batch related messages.
        """
        pending = len(self._buffers.get(chat_id, ()))
        return self._long_debounce if pending >= 2 else self._short_debounce

    async def _flush(self, chat_id: str) -> None:
        """Wait for adaptive silence, then flush the buffer.

        Each ``send()`` sets the per-chat asyncio Event, resetting the debounce
        window. The window shortens to ``short_debounce`` while the buffer
        holds a single message and widens to ``long_debounce`` once a burst
        accumulates.
        """
        event = self._events.setdefault(chat_id, asyncio.Event())
        try:
            while True:
                event.clear()
                try:
                    await asyncio.wait_for(
                        event.wait(), timeout=self._current_debounce(chat_id)
                    )
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
