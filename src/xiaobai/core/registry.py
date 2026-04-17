"""Channel registry — routes a ``chat_id`` to the channel that owns it."""

from __future__ import annotations

from typing import Iterable

from .channel import Channel


class ChannelRegistry:
    """Holds all active channels and routes by ``chat_id``.

    First-match semantics: iterate channels in registration order, return the
    first whose ``.owns(chat_id)`` returns True. This lets the WeChat channel
    (checking ``endswith("@im.wechat")``) shadow the Feishu channel
    (checking ``startswith("oc_")``) even though a WeChat chat_id may also
    start with ``o`` — as long as WeChat is registered first or Feishu's
    ``owns`` check excludes the WeChat suffix.
    """

    def __init__(self) -> None:
        self._channels: list[Channel] = []

    def add(self, channel: Channel) -> None:
        """Register a channel. Later additions have lower priority."""
        self._channels.append(channel)

    def get(self, chat_id: str) -> Channel:
        """Return the channel that owns ``chat_id`` or raise KeyError."""
        for ch in self._channels:
            if ch.owns(chat_id):
                return ch
        raise KeyError(f"No channel owns chat_id: {chat_id!r}")

    def list_channels(self) -> list[Channel]:
        return list(self._channels)

    def __iter__(self) -> Iterable[Channel]:
        return iter(self._channels)
