"""Channel-agnostic card protocol.

The Feishu ``CardManager`` (channels/feishu/cards.py, 566 lines) couples
two things: (1) the *concept* of progress cards — "some streaming status
surface that can be created, updated, and finalized with a final body",
and (2) the specific CardKit sequence-number state machine that
materialises it on Feishu.

Other channels (WeChat, future iMessage / Telegram / Discord) have no
CardKit but still want a progress surface — typically "edit a previously
sent message" or "send a follow-up". This protocol describes the
operations every channel must expose so the MCP tool handlers
(``reply_card``) can target them uniformly.

The Feishu implementation already matches this shape; channels that
don't natively support live updates can degrade to sending one final
message when ``finalize`` is called (see :class:`StatelessCardService`).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CardService(Protocol):
    """Operations every channel must provide to handle ``reply_card``."""

    def register_pending(
        self, request_id: str, chat_id: str, reply_to_message_id: str
    ) -> None:
        """Remember that ``request_id`` may later produce a card.

        Called once per inbound user message. The channel may defer actual
        card creation until ``create_card`` is invoked.
        """

    def cancel_pending(self, request_id: str) -> None:
        """Drop the pending slot for ``request_id`` without creating a card."""

    async def create_card(
        self, request_id: str, status: str, text: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Create (or initialize) a live card for the given request."""

    async def update_card(
        self, request_id: str, status: str, text: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Update an in-flight card's status / body."""

    async def finalize_card(self, request_id: str, text: str) -> dict[str, Any]:
        """Settle the card with its final body and stop live updates."""

    async def cleanup_stale_cards(self) -> int:
        """Drop any cards that exceeded their TTL without being finalized."""


class StatelessCardService:
    """Fallback implementation for channels without live-card support.

    Behavior:
    - ``register_pending`` / ``cancel_pending`` are no-ops
    - ``create_card`` / ``update_card`` do nothing (discard intermediate updates)
    - ``finalize_card`` sends the final text as a plain message

    Wire this into any channel that hasn't implemented real cards yet so
    ``reply_card`` from Claude still produces visible output — just without
    the streaming progress surface.
    """

    def __init__(self, send_text: Any) -> None:
        """``send_text(chat_id, text)`` is the underlying message sender."""
        self._send_text = send_text
        self._pending: dict[str, str] = {}  # request_id -> chat_id

    def register_pending(
        self, request_id: str, chat_id: str, reply_to_message_id: str
    ) -> None:
        self._pending[request_id] = chat_id

    def cancel_pending(self, request_id: str) -> None:
        self._pending.pop(request_id, None)

    async def create_card(
        self, request_id: str, status: str, text: str, **_kwargs: Any
    ) -> dict[str, Any]:
        return {"status": "ok", "mode": "stateless"}

    async def update_card(
        self, request_id: str, status: str, text: str, **_kwargs: Any
    ) -> dict[str, Any]:
        return {"status": "ok", "mode": "stateless"}

    async def finalize_card(
        self, request_id: str, text: str
    ) -> dict[str, Any]:
        chat_id = self._pending.pop(request_id, None)
        if chat_id is None:
            return {"status": "error", "message": "unknown request_id"}
        return await self._send_text(chat_id, text)

    async def cleanup_stale_cards(self) -> int:
        # Stateless — nothing to expire.
        return 0
