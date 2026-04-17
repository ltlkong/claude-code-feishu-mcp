"""Gemini CLI provider adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .base import DispatchToolFn, ProviderEvent, ProviderToolCall

logger = logging.getLogger(__name__)

RunGeminiFn = Callable[[str], Awaitable[str]]


class GeminiCliProvider:
    """Provider that asks Gemini CLI for structured Xiaobai tool calls."""

    def __init__(
        self,
        *,
        dispatch_tool: DispatchToolFn,
        instructions: str,
        command: str,
        args: list[str],
        timeout_seconds: int,
        run_gemini: RunGeminiFn | None = None,
    ) -> None:
        self._dispatch_tool = dispatch_tool
        self._instructions = instructions
        self._command = command
        self._args = args
        self._timeout_seconds = timeout_seconds
        self._run_gemini = run_gemini or self._run_subprocess

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def handle_event(self, event: ProviderEvent) -> None:
        prompt = self._build_prompt(event)
        try:
            output = (await self._run_gemini(prompt)).strip()
        except TimeoutError:
            await self._reply_if_possible(event, "Gemini timed out.")
            return
        except Exception as e:
            logger.error("Gemini provider failed: %s", e)
            await self._reply_if_possible(event, "Gemini provider failed.")
            return

        calls = self._parse_tool_calls(output)
        if calls:
            for call in calls:
                await self._dispatch_tool(call.name, call.arguments)
            return

        if output:
            await self._reply_if_possible(event, output)

    async def _run_subprocess(self, prompt: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            "-p",
            prompt,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=self._timeout_seconds
        )
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip())
        return stdout.decode("utf-8", errors="replace")

    def _build_prompt(self, event: ProviderEvent) -> str:
        return (
            f"{self._instructions}\n\n"
            "You are handling one Xiaobai chat event.\n"
            "Return JSON only. Prefer tool_calls. Available tools include reply, "
            "reply_image, reply_file, reply_video, read_messages, send_reaction.\n\n"
            f"content: {event.content}\n"
            f"meta: {json.dumps(event.meta, ensure_ascii=False)}\n\n"
            "Response shape: "
            '{"tool_calls":[{"name":"reply","arguments":{"chat_id":"...","text":"..."}}]}'
        )

    def _parse_tool_calls(self, output: str) -> list[ProviderToolCall]:
        try:
            data = json.loads(output)
        except json.JSONDecodeError:
            return []

        raw_calls: list[dict[str, Any]]
        if isinstance(data, dict) and isinstance(data.get("tool_calls"), list):
            raw_calls = data["tool_calls"]
        elif (
            isinstance(data, dict)
            and data.get("name")
            and isinstance(data.get("arguments"), dict)
        ):
            raw_calls = [data]
        else:
            return []

        calls = []
        for raw in raw_calls:
            name = raw.get("name")
            arguments = raw.get("arguments")
            if isinstance(name, str) and isinstance(arguments, dict):
                calls.append(ProviderToolCall(name=name, arguments=arguments))
        return calls

    async def _reply_if_possible(self, event: ProviderEvent, text: str) -> None:
        chat_id = event.meta.get("chat_id", "")
        if not chat_id:
            logger.warning("Gemini output dropped because event has no chat_id")
            return
        await self._dispatch_tool("reply", {"chat_id": chat_id, "text": text})
