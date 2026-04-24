"""Model selector for routing events to cheap vs. expensive providers.

The audit flagged provider routing as "wired but aspirational" — the
infrastructure supports multiple providers but every event today goes
to whichever single provider XIAOBAI_PROVIDER picks at startup.

This module adds the hook:

- :class:`RouteDecision`: which provider key to use + reason
- :func:`select_model`: heuristic that inspects a ``ProviderEvent`` and
  picks ``"cheap"`` (Gemini-class) vs. ``"expensive"`` (Claude-class).
  Purely functional — no side effects, easy to test.

Today the default XiaobaiServer still uses a single provider; to activate
routing the server can build two providers and call ``select_model`` on
every inbound event. The policy below is intentionally conservative:
only obviously-batchable / obviously-low-value traffic gets routed to
the cheap side. Anything ambiguous stays on the expensive side.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .base import ProviderEvent

Tier = Literal["cheap", "expensive"]


@dataclass(frozen=True)
class RouteDecision:
    tier: Tier
    reason: str


# Heuristic thresholds — tune with real data once traces land.
_LARGE_TEXT_CHARS = 12_000
_HEARTBEAT_TYPES = frozenset({"heartbeat"})
_CHEAP_MESSAGE_TYPES = frozenset({
    "heartbeat",       # periodic nudge — minimal reasoning needed
    "reaction",        # ack of a reaction, small response
    "image_ocr_batch", # future: pure transcription work
})


def select_model(event: ProviderEvent) -> RouteDecision:
    """Pick ``cheap`` or ``expensive`` based on the event shape.

    Rules (in order):

    1. Explicit override in ``meta['_tier']`` wins (for tests / overrides).
    2. Known-cheap ``message_type`` (heartbeat, reaction, batch OCR) → cheap.
    3. Very large payloads (> ``_LARGE_TEXT_CHARS``) → expensive so the
       smarter model can summarize / reason across them.
    4. Everything else → expensive.

    Keep the policy conservative: a misroute toward the cheap tier is a
    quality regression, so we only route when we're confident.
    """
    meta = event.meta or {}
    override = meta.get("_tier")
    if override in ("cheap", "expensive"):
        return RouteDecision(override, reason="override")

    message_type = str(meta.get("message_type", ""))
    if message_type in _CHEAP_MESSAGE_TYPES:
        return RouteDecision("cheap", reason=f"message_type={message_type}")

    if message_type in _HEARTBEAT_TYPES:
        return RouteDecision("cheap", reason="heartbeat")

    content_len = len(event.content or "")
    if content_len > _LARGE_TEXT_CHARS:
        return RouteDecision(
            "expensive", reason=f"large_content={content_len}"
        )

    return RouteDecision("expensive", reason="default")
