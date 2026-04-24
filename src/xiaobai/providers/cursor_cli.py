"""Cursor Agent CLI provider adapter."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from .base import DispatchToolFn
from .cli_bridge import CliBridgeProvider

RunCursorFn = Callable[[str], Awaitable[str]]


class CursorCliProvider(CliBridgeProvider):
    """Cursor-specific wrapper over the shared CLI bridge."""

    def __init__(
        self,
        *,
        dispatch_tool: DispatchToolFn,
        instructions: str,
        command: str,
        args: list[str],
        prompt_flag: str,
        timeout_seconds: int,
        run_cursor: RunCursorFn | None = None,
    ) -> None:
        super().__init__(
            provider_name="Cursor",
            dispatch_tool=dispatch_tool,
            instructions=instructions,
            command=command,
            args=args,
            prompt_flag=prompt_flag,
            timeout_seconds=timeout_seconds,
            run_cli=run_cursor,
        )
