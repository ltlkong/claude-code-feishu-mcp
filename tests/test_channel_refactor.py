import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from xiaobai.channels.wechat.channel import WeChatChannel
from xiaobai.core.channel import Capabilities
from xiaobai.core.notifications import NotificationPipeline
from xiaobai.mcp_server import XiaobaiServer


class NotificationPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_message_appended_during_write_gets_flushed(self):
        writes = []
        first_write_started = asyncio.Event()
        release_first_write = asyncio.Event()

        async def write(content, meta):
            writes.append((content, dict(meta)))
            if content == "first":
                first_write_started.set()
                await release_first_write.wait()

        pipeline = NotificationPipeline(write, debounce_seconds=0.01)

        await pipeline.send("first", {"chat_id": "c1"})
        await asyncio.wait_for(first_write_started.wait(), timeout=1)
        await pipeline.send("second", {"chat_id": "c1"})
        release_first_write.set()

        async def wait_for_second_write():
            while len(writes) < 2:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_second_write(), timeout=1)
        self.assertEqual([content for content, _ in writes], ["first", "second"])


class ServerNotificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_notifications_before_pipeline_ready_are_queued_and_flushed(self):
        server = XiaobaiServer.__new__(XiaobaiServer)
        server._pipeline = None
        server._pending_notifications = []

        await server._send_channel_notification("hello", {"chat_id": "c1"})

        self.assertEqual(server._pending_notifications, [("hello", {"chat_id": "c1"})])

        class FakePipeline:
            def __init__(self):
                self.sent = []

            async def send(self, content, meta):
                self.sent.append((content, meta))

        pipeline = FakePipeline()
        server._pipeline = pipeline
        await server._flush_pending_notifications()

        self.assertEqual(pipeline.sent, [("hello", {"chat_id": "c1"})])
        self.assertEqual(server._pending_notifications, [])


class FeishuPostParsingTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_post_content_reads_language_wrapped_post_and_md_tags(self):
        server = XiaobaiServer.__new__(XiaobaiServer)
        server.feishu = type("Feishu", (), {"http": object(), "token": object()})()
        server.settings = type("Settings", (), {"temp_dir": Path("/tmp")})()

        payload = json.dumps({
            "zh_cn": {
                "title": "标题",
                "content": [[
                    {"tag": "md", "text": "**Hi**"},
                    {"tag": "text", "text": " there"},
                ]],
            }
        })

        rendered = await server._process_post_content(payload, "msg_1")
        self.assertEqual(rendered, "[Title: 标题]\n**Hi** there")


class CapabilityDispatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_video_returns_capability_error_without_calling_channel(self):
        server = XiaobaiServer.__new__(XiaobaiServer)

        class NoVideoChannel:
            capabilities = Capabilities(has_video=False)

            async def send_video(self, chat_id, path):
                raise AssertionError("send_video should not be called")

        class Registry:
            def get(self, chat_id):
                return NoVideoChannel()

        server.registry = Registry()

        result = await server._dispatch_tool(
            "reply_video",
            {"chat_id": "chat", "video_path": "/tmp/v.mp4"},
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("does not support reply_video", result["message"])


class WeChatVideoTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_video_uploads_and_sends_video_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            video = Path(tmp) / "clip.mp4"
            video.write_bytes(b"fake mp4")

            client = type("Client", (), {})()
            client.send_message = AsyncMock(return_value={"ok": True})

            channel = WeChatChannel.__new__(WeChatChannel)
            channel._get_client_for = lambda chat_id: client

            async def fake_upload_media(upload_client, path, media_type, to_user_id=""):
                self.assertIs(upload_client, client)
                self.assertEqual(path, video)
                self.assertEqual(media_type, 2)
                self.assertEqual(to_user_id, "u1@im.wechat")
                return {
                    "encrypt_query_param": "eqp",
                    "aes_key": "00112233445566778899aabbccddeeff",
                    "cipher_size": 123,
                    "raw_size": 99,
                }

            import xiaobai.channels.wechat.channel as wechat_channel

            original_upload_media = wechat_channel.upload_media
            wechat_channel.upload_media = fake_upload_media
            try:
                result = await channel.send_video("u1@im.wechat", str(video))
            finally:
                wechat_channel.upload_media = original_upload_media

            self.assertEqual(result, {"status": "ok"})
            client.send_message.assert_awaited_once()
            chat_id, item_list = client.send_message.await_args.args
            self.assertEqual(chat_id, "u1@im.wechat")
            self.assertEqual(item_list[0]["type"], 5)
            self.assertEqual(item_list[0]["video_item"]["media"]["encrypt_query_param"], "eqp")
