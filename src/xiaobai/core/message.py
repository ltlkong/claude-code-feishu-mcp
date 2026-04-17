"""Canonical message model.

Session 1 defines the dataclasses but does NOT enforce them on the callback
path — tool handlers still receive ``(content, meta)`` dicts to preserve
behavior verbatim. Session 2 migrates handlers to consume ``Message``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


@dataclass(frozen=True)
class ChannelAddress:
    """Addresses a chat across all channels."""

    channel: str                              # "feishu" | "wechat"
    chat_id: str
    chat_type: Literal["p2p", "group"]

    def uri(self) -> str:
        return f"{self.channel}://{self.chat_id}"


@dataclass
class Message:
    """Canonical representation of an inbound message event.

    Future shape (Session 2+). Presently unused on the callback path; the
    listeners still emit (content_str, meta_dict).
    """

    addr: ChannelAddress
    user_id: str
    msg_type: str           # text/image/audio/video/file/reaction/card_action/recall
    content: str            # rendered text; JSON-string for media-type payloads
    timestamp: datetime
    message_id: str
    request_id: str
    reply_to_id: str | None = None
    meta: dict = field(default_factory=dict)  # channel-specific extras
