import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaobai.channels.wechat.ilink import ILinkClient, ILinkProtocolError
from xiaobai.channels.wechat.listener import WeChatListener


class WeChatIlinkClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_updates_raises_protocol_error_for_errcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            client = ILinkClient(
                base_url="https://example.com",
                cdn_url="https://example.com",
                state_dir=Path(tmp),
            )

            class FakeResp:
                def raise_for_status(self):
                    return None

                def json(self):
                    return {"errcode": 1001, "errmsg": "invalid token"}

            async def fake_post(*args, **kwargs):
                return FakeResp()

            client._client.post = fake_post
            with self.assertRaises(ILinkProtocolError):
                await client.get_updates()

            await client.close()


class WeChatListenerPollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_protocol_error_uses_backoff(self):
        events = []

        async def on_message(content, meta):
            events.append((content, meta))

        class FakeClient:
            def __init__(self):
                self.calls = 0

            async def get_updates(self):
                self.calls += 1
                raise ILinkProtocolError(1001, "invalid token")

        listener = WeChatListener(FakeClient(), on_message)

        async def fake_backoff():
            listener._running = False

        with patch.object(listener, "_do_backoff", fake_backoff):
            await listener._poll_loop()

        self.assertEqual(events, [])

    async def test_listener_session_timeout_stops_polling(self):
        async def on_message(content, meta):
            return None

        class FakeClient:
            async def get_updates(self):
                raise ILinkProtocolError(-14, "session timeout")

        listener = WeChatListener(FakeClient(), on_message)
        listener._running = True

        backoff_called = False

        async def fake_backoff():
            nonlocal backoff_called
            backoff_called = True

        with patch.object(listener, "_do_backoff", fake_backoff):
            await listener._poll_loop()

        self.assertFalse(listener._running)
        self.assertFalse(backoff_called)

