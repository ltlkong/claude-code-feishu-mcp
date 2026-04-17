import unittest
from unittest.mock import patch

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

    async def test_single_tool_call_shorthand_dispatches_existing_tool(self):
        calls = []

        async def dispatch(name, arguments):
            calls.append((name, arguments))
            return {"status": "ok"}

        async def run_gemini(prompt):
            return '{"name":"send_reaction","arguments":{"message_id":"m1","emoji":"OK"}}'

        provider = GeminiCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="gemini",
            args=["--yolo"],
            timeout_seconds=5,
            run_gemini=run_gemini,
        )

        await provider.handle_event(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertEqual(calls, [("send_reaction", {"message_id": "m1", "emoji": "OK"})])

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

    async def test_malformed_tool_json_is_not_sent_as_plain_text(self):
        calls = []

        async def dispatch(name, arguments):
            calls.append((name, arguments))
            return {"status": "ok"}

        async def run_gemini(prompt):
            return '{"tool_calls":[{"name":"reply","arguments":{"chat_id":"老板p2p","text":"在呢老板" patterns=""}}]}'

        provider = GeminiCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="gemini",
            args=["--yolo"],
            timeout_seconds=5,
            run_gemini=run_gemini,
        )

        await provider.handle_event(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertEqual(calls, [])

    async def test_subprocess_includes_media_directories(self):
        captured = {}

        class FakeProc:
            returncode = 0

            async def communicate(self):
                return b'{"tool_calls":[]}', b""

        async def fake_create_subprocess_exec(*cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeProc()

        provider = GeminiCliProvider(
            dispatch_tool=lambda name, arguments: None,
            instructions="You are Xiaobai.",
            command="gemini",
            args=["--yolo"],
            timeout_seconds=5,
        )

        with patch("asyncio.create_subprocess_exec", fake_create_subprocess_exec):
            output = await provider._run_subprocess("prompt")

        self.assertEqual(output, '{"tool_calls":[]}')
        self.assertIn("--include-directories", captured["cmd"])
        include_value = captured["cmd"][captured["cmd"].index("--include-directories") + 1]
        self.assertIn("/tmp/feishu-channel", include_value)
        self.assertIn("/tmp/wechat-channel", include_value)

    async def test_plain_text_without_chat_id_is_dropped(self):
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

        await provider.handle_event(ProviderEvent("hello", {}))

        self.assertEqual(calls, [])

    async def test_provider_failure_does_not_post_failure_message(self):
        calls = []

        async def dispatch(name, arguments):
            calls.append((name, arguments))
            return {"status": "ok"}

        async def run_gemini(prompt):
            raise RuntimeError("Operation cancelled.")

        provider = GeminiCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="gemini",
            args=["--yolo"],
            timeout_seconds=5,
            run_gemini=run_gemini,
        )

        await provider.handle_event(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertEqual(calls, [])

    async def test_tool_dispatch_failure_does_not_raise_or_fallback_reply(self):
        calls = []

        async def dispatch(name, arguments):
            calls.append((name, arguments))
            raise KeyError("No channel owns chat_id")

        async def run_gemini(prompt):
            return '{"tool_calls":[{"name":"reply","arguments":{"chat_id":"bad","text":"hi"}}]}'

        provider = GeminiCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="gemini",
            args=["--yolo"],
            timeout_seconds=5,
            run_gemini=run_gemini,
        )

        await provider.handle_event(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertEqual(calls, [("reply", {"chat_id": "bad", "text": "hi"})])
