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

    def test_build_provider_rejects_unknown_provider(self):
        server = XiaobaiServer.__new__(XiaobaiServer)
        server.settings = type("Settings", (), {
            "xiaobai_provider": "unknown",
            "load_instructions": lambda self: "instructions",
        })()

        with self.assertRaises(ValueError):
            server._build_provider()
