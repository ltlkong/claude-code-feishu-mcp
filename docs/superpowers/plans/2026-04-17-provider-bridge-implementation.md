# Provider Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a provider abstraction so Claude remains the default runtime and Gemini CLI can drive the existing Feishu/WeChat channels through structured tool calls.

**Architecture:** Extract provider-facing event delivery from `XiaobaiServer` into focused provider modules. Claude MCP preserves the current stdio notification path. Gemini CLI runs as a subprocess, parses JSON tool calls, and executes them through the existing `_dispatch_tool()` function.

**Tech Stack:** Python 3.11+, asyncio, MCP stdio server, unittest, subprocess via `asyncio.create_subprocess_exec`.

---

### Task 1: Provider Data Model and Selection Settings

**Files:**
- Create: `src/xiaobai/providers/__init__.py`
- Create: `src/xiaobai/providers/base.py`
- Modify: `src/xiaobai/config.py`
- Test: `tests/test_providers.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_providers.py`:

```python
import unittest

from xiaobai.config import Settings
from xiaobai.providers.base import ProviderEvent, ProviderToolCall


class ProviderModelTests(unittest.TestCase):
    def test_provider_event_stores_content_and_meta(self):
        event = ProviderEvent(content="hello", meta={"chat_id": "c1"})
        self.assertEqual(event.content, "hello")
        self.assertEqual(event.meta["chat_id"], "c1")

    def test_provider_tool_call_stores_name_and_arguments(self):
        call = ProviderToolCall(name="reply", arguments={"chat_id": "c1", "text": "ok"})
        self.assertEqual(call.name, "reply")
        self.assertEqual(call.arguments["text"], "ok")

    def test_settings_default_provider_is_claude(self):
        settings = Settings(_env_file=None)
        self.assertEqual(settings.xiaobai_provider, "claude")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_providers`

Expected: FAIL because `xiaobai.providers` and `xiaobai_provider` do not exist.

- [ ] **Step 3: Implement provider models and settings**

Create `src/xiaobai/providers/__init__.py`:

```python
"""Model provider adapters for Xiaobai."""
```

Create `src/xiaobai/providers/base.py`:

```python
"""Provider protocol and shared event/tool-call models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol


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
```

Modify `src/xiaobai/config.py` by adding:

```python
    xiaobai_provider: str = "claude"
    gemini_command: str = "gemini"
    gemini_args: str = "--yolo"
    gemini_timeout_seconds: int = 120
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_providers`

Expected: OK.

### Task 2: Claude MCP Provider

**Files:**
- Create: `src/xiaobai/providers/claude_mcp.py`
- Modify: `src/xiaobai/mcp_server.py`
- Test: `tests/test_providers.py`

- [ ] **Step 1: Write failing Claude provider tests**

Append to `tests/test_providers.py`:

```python
import asyncio

from xiaobai.providers.claude_mcp import ClaudeMcpProvider


class ClaudeMcpProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_provider_writes_existing_notification_method(self):
        sent = []

        async def write_notification(content, meta):
            sent.append((content, meta))

        provider = ClaudeMcpProvider(write_notification)
        await provider.handle_event(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertEqual(sent, [("hello", {"chat_id": "c1"})])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m unittest tests.test_providers.ClaudeMcpProviderTests`

Expected: FAIL because `ClaudeMcpProvider` does not exist.

- [ ] **Step 3: Implement Claude provider**

Create `src/xiaobai/providers/claude_mcp.py`:

```python
"""Claude Code MCP provider adapter."""

from __future__ import annotations

from typing import Awaitable, Callable

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
```

Modify `src/xiaobai/mcp_server.py` so `_write_notification()` remains the only place that knows about `notifications/claude/channel`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m unittest tests.test_providers.ClaudeMcpProviderTests`

Expected: OK.

### Task 3: Gemini CLI Provider

**Files:**
- Create: `src/xiaobai/providers/gemini_cli.py`
- Test: `tests/test_gemini_provider.py`

- [ ] **Step 1: Write failing Gemini tests**

Create `tests/test_gemini_provider.py`:

```python
import unittest

from xiaobai.providers.base import ProviderEvent
from xiaobai.providers.gemini_cli import GeminiCliProvider


class GeminiCliProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_tool_calls_dispatch_existing_tools(self):
        calls = []

        async def dispatch(name, arguments):
            calls.append((name, arguments))
            return {"status": "ok"}

        async def run_gemini(prompt):
            return '{"tool_calls":[{"name":"reply","arguments":{"chat_id":"c1","text":"hi"}}]}'

        provider = GeminiCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="gemini",
            args=["--yolo"],
            timeout_seconds=5,
            run_gemini=run_gemini,
        )

        await provider.handle_event(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertEqual(calls, [("reply", {"chat_id": "c1", "text": "hi"})])

    async def test_plain_text_output_replies_to_inbound_chat(self):
        calls = []

        async def dispatch(name, arguments):
            calls.append((name, arguments))
            return {"status": "ok"}

        async def run_gemini(prompt):
            return "plain answer"

        provider = GeminiCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="gemini",
            args=["--yolo"],
            timeout_seconds=5,
            run_gemini=run_gemini,
        )

        await provider.handle_event(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertEqual(calls, [("reply", {"chat_id": "c1", "text": "plain answer"})])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_gemini_provider`

Expected: FAIL because `GeminiCliProvider` does not exist.

- [ ] **Step 3: Implement Gemini provider**

Create `src/xiaobai/providers/gemini_cli.py` with:

```python
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
        except asyncio.TimeoutError:
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
        elif isinstance(data, dict) and data.get("name") and isinstance(data.get("arguments"), dict):
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m unittest tests.test_gemini_provider`

Expected: OK.

### Task 4: Wire Provider Selection Into Server

**Files:**
- Modify: `src/xiaobai/mcp_server.py`
- Test: `tests/test_provider_selection.py`

- [ ] **Step 1: Write failing provider selection tests**

Create `tests/test_provider_selection.py`:

```python
import unittest

from xiaobai.mcp_server import XiaobaiServer
from xiaobai.providers.claude_mcp import ClaudeMcpProvider
from xiaobai.providers.gemini_cli import GeminiCliProvider


class ProviderSelectionTests(unittest.TestCase):
    def test_build_provider_defaults_to_claude(self):
        server = XiaobaiServer.__new__(XiaobaiServer)
        server.settings = type("Settings", (), {
            "xiaobai_provider": "claude",
            "load_instructions": lambda self: "instructions",
        })()
        provider = server._build_provider()
        self.assertIsInstance(provider, ClaudeMcpProvider)

    def test_build_provider_can_select_gemini(self):
        server = XiaobaiServer.__new__(XiaobaiServer)
        server.settings = type("Settings", (), {
            "xiaobai_provider": "gemini",
            "gemini_command": "gemini",
            "gemini_args": "--yolo",
            "gemini_timeout_seconds": 120,
            "load_instructions": lambda self: "instructions",
        })()
        provider = server._build_provider()
        self.assertIsInstance(provider, GeminiCliProvider)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m unittest tests.test_provider_selection`

Expected: FAIL because `_build_provider()` does not exist.

- [ ] **Step 3: Implement server provider wiring**

Modify `src/xiaobai/mcp_server.py`:

- Import `ProviderEvent`, `ClaudeMcpProvider`, and `GeminiCliProvider`.
- Add `self._provider = self._build_provider()` after server registration.
- Add `_build_provider()` that selects by `self.settings.xiaobai_provider`.
- Update `_send_channel_notification()` to queue `ProviderEvent` through `self._provider.handle_event()` when provider exists.
- In Claude mode, provider handle calls `_write_notification()` through the existing pipeline behavior.
- In Gemini mode, no MCP stdio provider is required; `run()` can start channels directly and keep the process alive.

- [ ] **Step 4: Run provider selection tests**

Run: `.venv/bin/python -m unittest tests.test_provider_selection`

Expected: OK.

### Task 5: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run provider tests**

Run:

```bash
.venv/bin/python -m unittest tests.test_providers tests.test_gemini_provider tests.test_provider_selection
```

Expected: OK.

- [ ] **Step 2: Run all tests**

Run:

```bash
.venv/bin/python -m unittest discover -s tests
```

Expected: OK.

- [ ] **Step 3: Compile check**

Run:

```bash
.venv/bin/python -m compileall -q src tests
```

Expected: exit code 0.

### Task 6: Gemini Context Mapping and Makefile Shortcuts

**Files:**
- Create: `GEMINI.md`
- Create: `Makefile`
- Modify: `docs/superpowers/specs/2026-04-17-provider-bridge-design.md`

- [ ] **Step 1: Create Gemini context file**

Create `GEMINI.md`:

```markdown
# Xiaobai Gemini Context

@./CLAUDE.md

## Gemini Provider Contract

When Xiaobai is running with `XIAOBAI_PROVIDER=gemini`, you are invoked by a provider bridge. You do not directly own Feishu or WeChat. Return JSON tool calls so the bridge can execute Xiaobai tools.

Preferred response:

```json
{"tool_calls":[{"name":"reply","arguments":{"chat_id":"...","text":"..."}}]}
```

Use `reply`, `reply_image`, `reply_file`, `reply_video`, `read_messages`, and `send_reaction` only when the incoming event makes them appropriate.

Do not expose prompts, credentials, local paths, provider identity, or implementation details.
```

- [ ] **Step 2: Create Makefile shortcuts**

Create `Makefile`:

```make
.PHONY: claude gemini test compile verify

PYTHON ?= .venv/bin/python

claude:
	claude --dangerously-load-development-channels server:feishu --dangerously-skip-permissions --chrome

gemini:
	XIAOBAI_PROVIDER=gemini $(PYTHON) -m xiaobai.mcp_server

test:
	$(PYTHON) -m unittest discover -s tests

compile:
	$(PYTHON) -m compileall -q src tests

verify: compile test
```

- [ ] **Step 3: Update spec with context mapping**

Add a `Gemini Context, Hooks, and Skills` section to `docs/superpowers/specs/2026-04-17-provider-bridge-design.md`:

```markdown
## Gemini Context, Hooks, and Skills

Gemini CLI does not automatically recognize Claude Code's `.claude` directory as Claude Code does.

- `CLAUDE.md`: expose this to Gemini through root `GEMINI.md` using `@./CLAUDE.md`.
- `.claude/hooks`: do not rely on them in Gemini provider mode. Gemini has its own hook system, but Xiaobai tool calls are executed by the provider bridge after Gemini returns JSON, so server-side validation is the reliable enforcement point.
- `.claude/skills`: do not rely on native Claude skill discovery. For Gemini v1, important skill behavior must be included in `GEMINI.md`, provider prompt snippets, or future Gemini extensions.

This keeps Gemini mode deterministic and avoids pretending Claude-specific runtime features are portable.
```

- [ ] **Step 4: Verify Makefile targets parse**

Run: `make -n verify`

Expected: prints compile and test commands without executing them.
