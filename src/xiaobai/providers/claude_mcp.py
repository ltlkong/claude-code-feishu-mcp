"""Claude Code MCP provider adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .base import ProviderEvent


WriteNotificationFn = Callable[[str, dict], Awaitable[None]]


class ClaudeMcpProvider:
    """Provider that preserves the existing Claude Code notification path."""

    def __init__(self, write_notification: WriteNotificationFn) -> None:
        self._write_notification = write_notification

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def handle_event(self, event: ProviderEvent) -> None:
        await self._write_notification(event.content, event.meta)
