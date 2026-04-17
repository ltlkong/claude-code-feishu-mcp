"""Provider protocol and shared event/tool-call models."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderEvent:
    content: str
    meta: dict[str, Any]


@dataclass(frozen=True)
class ProviderToolCall:
    name: str
    arguments: dict[str, Any]


DispatchToolFn = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class Provider(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def handle_event(self, event: ProviderEvent) -> None: ...
