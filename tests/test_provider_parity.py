import unittest

from xiaobai.providers.base import ProviderEvent
from xiaobai.providers.cursor_cli import CursorCliProvider
from xiaobai.providers.cli_bridge import CliBridgeProvider


class ProviderParityTests(unittest.TestCase):
    def test_cursor_prompt_contains_tool_catalog(self):
        async def dispatch(name, arguments):
            return {"status": "ok"}

        cursor = CursorCliProvider(
            dispatch_tool=dispatch,
            instructions="You are Xiaobai.",
            command="cursor-agent",
            args=["--json"],
            prompt_flag="-p",
            timeout_seconds=5,
        )
        prompt = cursor._build_prompt(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertIn("reply_card", prompt)
        self.assertIn("manage_heartbeat", prompt)

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
