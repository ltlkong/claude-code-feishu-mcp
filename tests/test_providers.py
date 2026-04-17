import unittest

from xiaobai.config import Settings
from xiaobai.providers.claude_mcp import ClaudeMcpProvider
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


class ClaudeMcpProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_claude_provider_writes_existing_notification_method(self):
        sent = []

        async def write_notification(content, meta):
            sent.append((content, meta))

        provider = ClaudeMcpProvider(write_notification)
        await provider.handle_event(ProviderEvent("hello", {"chat_id": "c1"}))

        self.assertEqual(sent, [("hello", {"chat_id": "c1"})])
