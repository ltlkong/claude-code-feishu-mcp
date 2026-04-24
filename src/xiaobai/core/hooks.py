"""Lightweight hook runner for non-Claude providers."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class HookRunner:
    """Runs command hooks from `.claude/settings.json`."""

    def __init__(
        self,
        settings_path: Path | None = None,
        *,
        command_timeout_seconds: int = 10,
    ) -> None:
        self._settings_path = settings_path or Path(".claude/settings.json")
        self._command_timeout_seconds = command_timeout_seconds
        self._hooks = self._load_hooks()

    def _load_hooks(self) -> dict[str, list[str]]:
        if not self._settings_path.is_file():
            return {}
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Failed to parse hook settings %s: %s", self._settings_path, e)
            return {}

        hooks_data = data.get("hooks", {})
        if not isinstance(hooks_data, dict):
            return {}

        hooks: dict[str, list[str]] = {}
        for event_name, entries in hooks_data.items():
            if not isinstance(entries, list):
                continue
            commands: list[str] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                items = entry.get("hooks", [])
                if not isinstance(items, list):
                    continue
                for hook in items:
                    if not isinstance(hook, dict):
                        continue
                    if hook.get("type") != "command":
                        continue
                    command = hook.get("command")
                    if isinstance(command, str) and command.strip():
                        commands.append(command)
            if commands:
                hooks[event_name] = commands
        return hooks

    async def run_post_tool_use(
        self,
        *,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_response: Any,
    ) -> None:
        payload = {
            "tool_name": tool_name,
            "tool_input": tool_input,
            "tool_response": tool_response,
        }
        await self._run_event("PostToolUse", payload)

    async def _run_event(self, event_name: str, payload: dict[str, Any]) -> None:
        commands = self._hooks.get(event_name, [])
        if not commands:
            return

        input_bytes = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        for command in commands:
            try:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(
                    proc.communicate(input=input_bytes),
                    timeout=self._command_timeout_seconds,
                )
                if proc.returncode != 0:
                    logger.warning(
                        "Hook command failed (%s): %s",
                        proc.returncode,
                        stderr.decode("utf-8", errors="replace").strip(),
                    )
            except TimeoutError:
                logger.warning("Hook command timed out for event %s", event_name)
            except Exception as e:
                logger.warning("Hook command execution failed for %s: %s", event_name, e)
