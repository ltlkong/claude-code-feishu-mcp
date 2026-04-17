"""Channel protocol + Capabilities dataclass.

A ``Channel`` is an adapter over one messaging platform (Feishu, WeChat, ...).
Each implementation wraps its underlying listener / API client and exposes a
uniform surface for the tool layer (Session 2) to dispatch against.

Callback shape (``OnMessageCallback``) matches the existing notification
payload (``content``, ``meta``) — the full refactor to canonical ``Message``
dataclasses is deferred to Session 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable


@dataclass(frozen=True)
class Capabilities:
    """Which optional send_* operations a channel supports."""

    has_cards: bool = False
    has_reactions: bool = False
    has_audio: bool = False        # TTS out
    has_video: bool = False
    has_post: bool = False         # rich formatted messages (Feishu post)
    has_reply_to: bool = False
    has_read_history_api: bool = False  # if False, history comes from local log


# Incoming message callback: (content_or_payload, meta).
# ``content`` is the rendered text / JSON payload. ``meta`` carries request_id,
# chat_id, user_id, message_type, etc. This matches the shape server.py already
# passes through ``_send_channel_notification``.
OnMessageCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


@runtime_checkable
class Channel(Protocol):
    """Uniform platform adapter.

    Implementations should set ``id`` (short platform label) and
    ``capabilities`` as instance attributes, and implement the methods below.
    For methods guarded by a capability flag (video/audio/post/reactions/
    read_history) the registry / tool layer should check
    ``channel.capabilities.has_xxx`` before calling.
    """

    id: str                    # "feishu" | "wechat"
    capabilities: Capabilities

    def owns(self, chat_id: str) -> bool:
        """Return True if this channel handles the given chat_id."""
        ...

    async def start(self, loop, on_message: OnMessageCallback) -> None:
        """Begin receiving messages. ``loop`` is the main asyncio event loop.

        Listeners that run in a background thread (lark-oapi) must schedule
        the callback on ``loop`` via ``run_coroutine_threadsafe``.
        """
        ...

    async def stop(self) -> None:
        """Cancel background tasks and release resources."""
        ...

    # ── Required send_* operations ────────────────────────────────

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> dict: ...

    async def send_image(self, chat_id: str, path: str) -> dict: ...

    async def send_file(self, chat_id: str, path: str) -> dict: ...

    # ── Capability-gated send_* operations ────────────────────────

    async def send_video(self, chat_id: str, path: str) -> dict: ...

    async def send_audio_tts(self, chat_id: str, text: str) -> dict: ...

    async def send_post(
        self, chat_id: str, title: str, content: list
    ) -> dict: ...

    async def send_reaction(self, message_id: str, emoji: str) -> dict: ...

    async def read_history(self, chat_id: str, count: int) -> list[dict]: ...
