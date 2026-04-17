import unittest
from unittest.mock import patch

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

    def test_build_provider_rejects_unknown_provider(self):
        server = XiaobaiServer.__new__(XiaobaiServer)
        server.settings = type("Settings", (), {
            "xiaobai_provider": "unknown",
            "load_instructions": lambda self: "instructions",
        })()

        with self.assertRaises(ValueError):
            server._build_provider()

    def test_provider_dispatch_resolves_chat_aliases_before_tool_dispatch(self):
        server = XiaobaiServer.__new__(XiaobaiServer)
        server._short_ids = type("ShortIds", (), {
            "resolve_message": lambda self, value: value,
            "resolve_request": lambda self, value: value,
        })()
        calls = []

        async def fake_dispatch(name, arguments):
            calls.append((name, arguments))
            return {"status": "ok"}

        server._dispatch_tool = fake_dispatch

        async def run_test():
            with patch("xiaobai.mcp_server.tools_profile.resolve_alias", return_value="oc_real"):
                return await server._dispatch_provider_tool(
                    "reply", {"chat_id": "老板p2p", "text": "hi"}
                )

        import asyncio
        result = asyncio.run(run_test())

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(calls, [("reply", {"chat_id": "oc_real", "text": "hi"})])
