"""reply_card — Feishu-specific card progress/finalize tool.

Thin wrapper over :class:`CardManager.update_card` / ``finalize_card``. The
central dispatcher also reads ``cards._cards`` / ``cards._origins`` to find
the chat_id so ``_mark_reply`` can record the last reply time.
"""

from __future__ import annotations

import logging

from ..channels.feishu.cards import CardManager

logger = logging.getLogger(__name__)


async def reply_card(
    cards: CardManager,
    request_id: str,
    text: str,
    status: str = "",
    done: bool = False,
    emoji: str = "⏳",
    template: str = "indigo",
) -> dict:
    """Update or finalize the card tied to ``request_id``."""
    if done:
        return await cards.finalize_card(request_id, text)
    return await cards.update_card(
        request_id, status, text, emoji=emoji, template=template
    )


def resolve_card_chat_id(cards: CardManager, request_id: str) -> str:
    """Return the chat_id for a card (for ``_mark_reply`` bookkeeping)."""
    state = cards._cards.get(request_id)
    if state:
        return state.chat_id
    origin = cards._origins.get(request_id)
    if origin:
        return origin[0]
    return ""
