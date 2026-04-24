import unittest

from xiaobai.providers.base import ProviderEvent
from xiaobai.providers.cursor_cli import CursorCliProvider
from xiaobai.providers.gemini_cli import GeminiCliProvider
from xiaobai.providers.cli_bridge import CliBridgeProvider


class ProviderParityTests(unittest.TestCase):
    def test_gemini_and_cursor_use_same_prompt_contract(self):
        async def dispatch(name, arguments):
            return {"status": "ok"}

        gemini = GeminiCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="gemini",
            args=["--yolo"],
            timeout_seconds=5,
        )
        cursor = CursorCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="cursor-agent",
            args=["--json"],
            prompt_flag="-p",
            timeout_seconds=5,
        )
        event = ProviderEvent("hello", {"chat_id": "c1"})

        gemini_prompt = gemini._build_prompt(event)
        cursor_prompt = cursor._build_prompt(event)

        self.assertEqual(gemini_prompt, cursor_prompt)
        self.assertIn("reply_card", gemini_prompt)
        self.assertIn("manage_heartbeat", gemini_prompt)

    def test_cli_bridge_includes_skills_context_when_provided(self):
        async def dispatch(name, arguments):
            return {"status": "ok"}

        provider = CliBridgeProvider(
            provider_name="Test",
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="echo",
            args=[],
            prompt_flag="-p",
            timeout_seconds=5,
            skills_context="### sample-skill\nAlways do X.",
        )
        prompt = provider._build_prompt(ProviderEvent("hello", {"chat_id": "c1"}))
        self.assertIn("sample-skill", prompt)
        self.assertIn("Always do X.", prompt)
