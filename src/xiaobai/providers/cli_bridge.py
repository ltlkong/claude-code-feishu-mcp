"""Shared CLI bridge for non-Claude providers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from .base import DispatchToolFn, ProviderEvent, ProviderToolCall
from ..core.skills import build_skills_context

logger = logging.getLogger(__name__)

RunCliFn = Callable[[str], Awaitable[str]]

DEFAULT_TOOL_NAMES = (
    "reply",
    "reply_card",
    "reply_file",
    "reply_image",
    "reply_video",
    "reply_post",
    "reply_audio",
    "create_reminder",
    "list_reminders",
    "delete_reminder",
    "create_doc",
    "create_bitable",
    "update_profile",
    "read_messages",
    "send_reaction",
    "bitable_records",
    "manage_task",
    "manage_heartbeat",
    "wechat_login_qr",
    "search_image",
    "search_docs",
    "get_user_info",
)


class CliBridgeProvider:
    """Shared JSON tool-call bridge for Gemini/Cursor-like CLIs."""

    def __init__(
        self,
        *,
        provider_name: str,
        dispatch_tool: DispatchToolFn,
        instructions: str,
        command: str,
        args: list[str],
        prompt_flag: str,
        timeout_seconds: int,
        include_directories: str = "",
        run_cli: RunCliFn | None = None,
        skills_context: str | None = None,
    ) -> None:
        self._provider_name = provider_name
        self._dispatch_tool = dispatch_tool
        self._instructions = instructions
        self._command = command
        self._args = args
        self._prompt_flag = prompt_flag
        self._timeout_seconds = timeout_seconds
        self._include_directories = include_directories.strip()
        self._run_cli = run_cli or self._run_subprocess
        self._skills_context = (
            build_skills_context() if skills_context is None else skills_context
        )

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def handle_event(self, event: ProviderEvent) -> None:
        prompt = self._build_prompt(event)
        try:
            output = (await self._run_cli(prompt)).strip()
        except TimeoutError:
            logger.error("%s provider timed out", self._provider_name)
            return
        except Exception as e:
            logger.error("%s provider failed: %s", self._provider_name, e)
            return

        calls = self._parse_tool_calls(output)
        if calls:
            for call in calls:
                try:
                    await self._dispatch_tool(call.name, call.arguments)
                except Exception as e:
                    logger.error(
                        "%s tool dispatch failed for %s: %s",
                        self._provider_name,
                        call.name,
                        e,
                    )
            return

        if self._looks_like_tool_response(output):
            logger.error(
                "%s returned invalid tool-call JSON: %s",
                self._provider_name,
                output[:500],
            )
            return

        if output:
            await self._reply_if_possible(event, output)

    async def _run_subprocess(self, prompt: str) -> str:
        cmd = [self._command, *self._args]
        if self._include_directories:
            cmd.extend(["--include-directories", self._include_directories])
        cmd.extend([self._prompt_flag, prompt])
        proc = await asyncio.create_subprocess_exec(
            *cmd,
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
        tools = ", ".join(DEFAULT_TOOL_NAMES)
        skills_block = (
            f"\n\n{self._skills_context}\n" if self._skills_context else ""
        )
        return (
            f"{self._instructions}\n\n"
            "You are handling one Xiaobai chat event.\n"
            "Return VALID JSON only. Do not return markdown fences. Do not add "
            "fields outside the schema. Prefer tool_calls.\n"
            f"Available tools: {tools}\n\n"
            f"{skills_block}"
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

    def _looks_like_tool_response(self, output: str) -> bool:
        stripped = output.lstrip()
        return (
            stripped.startswith("{")
            and (
                '"tool_calls"' in stripped
                or '"name"' in stripped
                or '"arguments"' in stripped
            )
        )

    async def _reply_if_possible(self, event: ProviderEvent, text: str) -> None:
        chat_id = event.meta.get("chat_id", "")
        if not chat_id:
            logger.warning(
                "%s output dropped because event has no chat_id", self._provider_name
            )
            return
        await self._dispatch_tool("reply", {"chat_id": chat_id, "text": text})
