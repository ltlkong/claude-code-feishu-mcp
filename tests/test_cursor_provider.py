import unittest

from xiaobai.providers.base import ProviderEvent
from xiaobai.providers.cursor_cli import CursorCliProvider


class CursorCliProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_json_tool_calls_dispatch_existing_tools(self):
        calls = []

        async def dispatch(name, arguments):
            calls.append((name, arguments))
            return {"status": "ok"}

        async def run_cursor(prompt):
            return '{"tool_calls":[{"name":"reply","arguments":{"chat_id":"c1","text":"hi"}}]}'

        provider = CursorCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="cursor-agent",
            args=["--json"],
            prompt_flag="-p",
            timeout_seconds=5,
            run_cursor=run_cursor,
        )

        await provider.handle_event(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertEqual(calls, [("reply", {"chat_id": "c1", "text": "hi"})])

    async def test_plain_text_output_replies_to_inbound_chat(self):
        calls = []

        async def dispatch(name, arguments):
            calls.append((name, arguments))
            return {"status": "ok"}

        async def run_cursor(prompt):
            return "plain answer"

        provider = CursorCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="cursor-agent",
            args=["--json"],
            prompt_flag="-p",
            timeout_seconds=5,
            run_cursor=run_cursor,
        )

        await provider.handle_event(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertEqual(calls, [("reply", {"chat_id": "c1", "text": "plain answer"})])
