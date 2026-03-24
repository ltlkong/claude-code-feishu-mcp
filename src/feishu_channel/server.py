"""MCP server entry point for the Feishu Channel.

Declares claude/channel capability, exposes update_status/reply/reply_file tools,
starts Feishu WebSocket listener in background, connects to Claude Code via stdio.
"""

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path

from mcp.server.lowlevel import Server
import mcp.server.stdio
from mcp import types

from .config import Settings
from .card import CardManager
from .feishu import FeishuListener
from .media import cleanup_old_files, download_image, download_audio, download_file, text_to_speech
from .reminder import create_reminder, list_reminders, delete_reminder, SCHEDULED_DIR
from .heartbeat import heartbeat_loop, mark_activity, configure_inactivity

logger = logging.getLogger(__name__)

# Load instructions from CLAUDE.md at project root — the bot's personality and behavioral rules.
# Falls back to a minimal default if the file doesn't exist.
_instructions_path = os.path.join(os.path.dirname(__file__), "..", "..", "CLAUDE.md")
if os.path.isfile(_instructions_path):
    with open(_instructions_path) as _f:
        INSTRUCTIONS = _f.read()
else:
    INSTRUCTIONS = "You are a helpful assistant on Feishu. Respond to messages and use the provided tools."

TOOLS = [
    types.Tool(
        name="update_status",
        description="Update the Feishu card with your current status and description. Call this to show the user what you're doing.",
        inputSchema={
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "The request_id from the inbound message"},
                "status": {"type": "string", "description": "Short status shown in card header, e.g. 'Thinking...', 'Searching...', 'Writing code...'"},
                "text": {"type": "string", "description": "Description of what you're currently doing"},
                "emoji": {"type": "string", "description": "Emoji for the header, e.g. '🔍', '💻', '🎨', '⏳'. Choose based on what you're doing.", "default": "⏳"},
                "template": {"type": "string", "description": "Header color theme: blue, wathet, turquoise, green, yellow, orange, red, carmine, violet, purple, indigo, grey, default", "default": "indigo"},
            },
            "required": ["request_id", "status", "text"],
        },
    ),
    types.Tool(
        name="reply",
        description="Send final response and finalize the card. Call this when done.",
        inputSchema={
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "The request_id from the inbound message"},
                "text": {"type": "string", "description": "Final response text"},
            },
            "required": ["request_id", "text"],
        },
    ),
    types.Tool(
        name="reply_file",
        description="Upload and send a file to a Feishu chat.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Feishu chat to send to"},
                "file_path": {"type": "string", "description": "Absolute path to the file"},
            },
            "required": ["chat_id", "file_path"],
        },
    ),
    types.Tool(
        name="reply_image",
        description="Send an image to a Feishu chat. The image is displayed inline (not as a file download). Use this for generated images, screenshots, etc.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Feishu chat to send to"},
                "image_path": {"type": "string", "description": "Absolute path to the image file (png, jpg, etc.)"},
            },
            "required": ["chat_id", "image_path"],
        },
    ),
    types.Tool(
        name="reply_video",
        description="Send a video to a Feishu chat. The video is displayed inline with a player (not as a file download). Auto-generates thumbnail from first frame.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Feishu chat to send to"},
                "video_path": {"type": "string", "description": "Absolute path to the video file (mp4, mov, etc.)"},
            },
            "required": ["chat_id", "video_path"],
        },
    ),
    types.Tool(
        name="reply_post",
        description="Send a rich text (post) message to Feishu with mixed text, images, videos, and links in one message. Images and videos can use local file paths (auto-uploaded).",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Feishu chat to send to"},
                "title": {"type": "string", "description": "Post title (optional, can be empty string)", "default": ""},
                "content": {
                    "type": "array",
                    "description": "Array of lines. Each line is an array of elements: {\"tag\":\"text\",\"text\":\"...\"} or {\"tag\":\"img\",\"image_key\":\"...\"} or {\"tag\":\"a\",\"text\":\"link text\",\"href\":\"url\"}",
                    "items": {
                        "type": "array",
                        "items": {"type": "object"}
                    }
                },
            },
            "required": ["chat_id", "content"],
        },
    ),
    types.Tool(
        name="reply_audio",
        description="Convert text to speech and send as a voice message to Feishu.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Feishu chat to send to"},
                "text": {"type": "string", "description": "Text to convert to speech (max 2000 chars)"},
            },
            "required": ["chat_id", "text"],
        },
    ),
    types.Tool(
        name="create_reminder",
        description="Create a scheduled reminder via cron. Two modes: simple (send fixed message) or smart (trigger Claude to think and decide response). Use standard cron expressions (minute hour day month weekday). Examples: '0 9 * * *' = daily 9am, '30 14 * * 1-5' = weekdays 2:30pm. Minimum interval: 1 minute.",
        inputSchema={
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "description": "Unique ID for this reminder, e.g. 'morning_meeting'"},
                "cron_expression": {"type": "string", "description": "Cron expression (5 fields: minute hour day month weekday)"},
                "chat_id": {"type": "string", "description": "Feishu chat_id to send the reminder to"},
                "message": {"type": "string", "description": "For simple mode: message text. For smart mode: the prompt/instruction for Claude."},
                "smart": {"type": "boolean", "description": "If true, triggers Claude to think and decide the response instead of sending a fixed message. Default: false.", "default": False},
                "max_runs": {"type": "integer", "description": "Max number of executions. 0 = unlimited (default). After N runs, the cron entry auto-deletes.", "default": 0},
            },
            "required": ["reminder_id", "cron_expression", "chat_id", "message"],
        },
    ),
    types.Tool(
        name="list_reminders",
        description="List all scheduled reminders.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
    types.Tool(
        name="delete_reminder",
        description="Delete a scheduled reminder by its ID.",
        inputSchema={
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "description": "ID of the reminder to delete"},
            },
            "required": ["reminder_id"],
        },
    ),
]


class FeishuChannel:
    """Orchestrates the MCP server, Feishu listener, and card manager."""

    def __init__(self):
        self.settings = Settings()
        self.cards = CardManager(
            self.settings.feishu_app_id,
            self.settings.feishu_app_secret,
            self.settings.stale_card_timeout_minutes,
        )
        # Shared httpx client — used by both card manager and media downloads
        self.http = self.cards._http
        self._write_stream = None  # Captured from stdio_server for sending notifications

        # Debounce: merge rapid messages from the same chat into one card
        self._debounce_delay = 2.0  # seconds
        self._debounce_pending: dict[str, asyncio.TimerHandle | None] = {}  # chat_id -> timer
        self._debounce_buffer: dict[str, list[tuple[str, str, dict]]] = {}  # chat_id -> [(content, request_id, meta)]

        # MCP server — decorator-based handler registration
        self.server = Server(name="feishu", version="0.1.0", instructions=INSTRUCTIONS)
        self._register_tools()

        # Feishu listener (initialized with callbacks)
        self.listener = FeishuListener(
            settings=self.settings,
            on_message=self._on_feishu_message,
            on_card_action=self._on_feishu_card_action,
        )

    def _register_tools(self):
        """Register MCP tools via decorator pattern on self.server."""
        cards = self.cards  # closure reference
        channel = self  # closure reference for reply_audio

        @self.server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            return TOOLS

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
            if name == "update_status":
                result = await cards.update_card(
                    arguments["request_id"], arguments["status"], arguments["text"],
                    emoji=arguments.get("emoji", "⏳"), template=arguments.get("template", "indigo"))
            elif name == "reply":
                result = await cards.finalize_card(arguments["request_id"], arguments["text"])
            elif name == "reply_file":
                result = await cards.upload_and_send_file(arguments["chat_id"], arguments["file_path"])
            elif name == "reply_image":
                result = await channel._handle_reply_image(arguments["chat_id"], arguments["image_path"])
            elif name == "reply_video":
                result = await channel._handle_reply_video(arguments["chat_id"], arguments["video_path"])
            elif name == "reply_post":
                result = await channel._handle_reply_post(
                    arguments["chat_id"], arguments.get("title", ""), arguments["content"])
            elif name == "reply_audio":
                result = await channel._handle_reply_audio(arguments["chat_id"], arguments["text"])
            elif name == "create_reminder":
                result = create_reminder(
                    arguments["reminder_id"],
                    arguments["cron_expression"],
                    arguments["chat_id"],
                    arguments["message"],
                    smart=arguments.get("smart", False),
                    max_runs=arguments.get("max_runs", 0),
                )
            elif name == "list_reminders":
                result = list_reminders()
            elif name == "delete_reminder":
                result = delete_reminder(arguments["reminder_id"])
            else:
                result = {"status": "error", "message": f"Unknown tool: {name}"}
            return [types.TextContent(type="text", text=json.dumps(result))]

    # ── Notification sending ─────────────────────────────────────
    # We capture the write_stream from stdio_server() and send raw
    # JSON-RPC notifications directly. This avoids needing a session
    # reference, which is only available inside request handler context.

    async def _send_channel_notification(self, content: str, meta: dict) -> None:
        """Send a channel notification to Claude Code via the MCP write stream."""
        if not self._write_stream:
            logger.warning("Write stream not ready, dropping notification")
            return

        from mcp.shared.session import SessionMessage
        from mcp.types import JSONRPCNotification, JSONRPCMessage

        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={"content": content, "meta": meta},
        )
        await self._write_stream.send(SessionMessage(JSONRPCMessage(notification)))

    # ── Feishu event callbacks ───────────────────────────────────

    async def _on_feishu_message(self, content: str, request_id: str, meta: dict) -> None:
        """Called when a Feishu message arrives. Debounces rapid messages from the same chat."""
        mark_activity()
        chat_id = meta["chat_id"]
        message_id = meta.get("message_id", "")

        # Handle media downloads immediately (don't delay file saves)
        message_type = meta["message_type"]
        if message_type in ("image", "audio", "file", "media"):
            content = await self._download_media(content, message_type, message_id)

        # Buffer the message
        if chat_id not in self._debounce_buffer:
            self._debounce_buffer[chat_id] = []
        self._debounce_buffer[chat_id].append((content, request_id, meta))

        # Cancel existing timer for this chat
        if chat_id in self._debounce_pending and self._debounce_pending[chat_id] is not None:
            self._debounce_pending[chat_id].cancel()

        # Set a new timer — fires after debounce_delay seconds of silence
        loop = asyncio.get_running_loop()
        self._debounce_pending[chat_id] = loop.call_later(
            self._debounce_delay,
            lambda cid=chat_id: asyncio.ensure_future(self._flush_debounce(cid)),
        )

    async def _flush_debounce(self, chat_id: str) -> None:
        """Flush buffered messages for a chat — create one card, send ONE merged notification."""
        messages = self._debounce_buffer.pop(chat_id, [])
        self._debounce_pending.pop(chat_id, None)
        if not messages:
            return

        # Use the LAST message's request_id and message_id for the card
        last_content, last_request_id, last_meta = messages[-1]
        last_message_id = last_meta.get("message_id", "")

        # Create ONE card for the batch
        await self.cards.create_card(last_request_id, chat_id, last_message_id)

        # Merge all buffered messages into ONE notification
        # Claude sees all messages at once as a single combined input
        if len(messages) == 1:
            # Single message — send as-is
            await self._send_channel_notification(last_content, last_meta)
        else:
            # Multiple messages — combine contents, use last meta (has the active request_id)
            merged_parts = []
            for content, request_id, meta in messages:
                merged_parts.append(content)
            merged_content = "\n".join(merged_parts)
            await self._send_channel_notification(merged_content, last_meta)

    async def _on_feishu_card_action(self, content: str, meta: dict) -> None:
        """Called when a card action (button click, form submit) arrives."""
        chat_id = meta["chat_id"]
        request_id = meta["request_id"]
        open_message_id = meta.get("open_message_id")

        if open_message_id:
            # Adopt the original card so Claude updates it in place
            await self.cards.adopt_card(request_id, chat_id, open_message_id)
        else:
            # Fallback: create a new card if we don't have the original message ID
            await self.cards.create_card(request_id, chat_id, "")

        await self._send_channel_notification(content, meta)

    # ── Image reply ────────────────────────────────────────────────

    async def _handle_reply_image(self, chat_id: str, image_path: str) -> dict:
        """Upload an image and send it as an inline image message."""
        import os
        if not os.path.exists(image_path):
            return {"status": "error", "message": f"File not found: {image_path}"}
        try:
            token = await self.cards._get_token()
            # Step 1: Upload image to get image_key
            with open(image_path, "rb") as f:
                resp = await self.http.post(
                    "https://open.feishu.cn/open-apis/im/v1/images",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"image_type": "message"},
                    files={"image": (os.path.basename(image_path), f)},
                )
            upload_data = resp.json()
            if upload_data.get("code") != 0:
                return {"status": "error", "message": f"Image upload failed: {upload_data}"}
            image_key = upload_data["data"]["image_key"]

            # Step 2: Send image message
            resp = await self.http.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": chat_id,
                    "msg_type": "image",
                    "content": json.dumps({"image_key": image_key}),
                },
            )
            send_data = resp.json()
            if send_data.get("code") != 0:
                return {"status": "error", "message": f"Image send failed: {send_data}"}
            return {"status": "ok", "image_key": image_key}
        except Exception as e:
            logger.error("Image reply failed: %s", e)
            return {"status": "error", "message": f"Image reply failed: {e}"}

    # ── Post (rich text) reply ────────────────────────────────────

    async def _upload_image_for_key(self, image_path: str) -> str | None:
        """Upload an image and return its image_key for use in post messages."""
        import os
        if not os.path.exists(image_path):
            return None
        try:
            token = await self.cards._get_token()
            with open(image_path, "rb") as f:
                resp = await self.http.post(
                    "https://open.feishu.cn/open-apis/im/v1/images",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"image_type": "message"},
                    files={"image": (os.path.basename(image_path), f)},
                )
            data = resp.json()
            if data.get("code") == 0:
                return data["data"]["image_key"]
        except Exception as e:
            logger.error("Image upload for post failed: %s", e)
        return None

    async def _upload_video_for_keys(self, video_path: str) -> tuple[str | None, str | None]:
        """Upload a video and its thumbnail, return (file_key, image_key)."""
        import os
        import subprocess as _sp
        if not os.path.exists(video_path):
            return None, None
        try:
            token = await self.cards._get_token()
            temp_dir = self.settings.temp_dir

            # Extract thumbnail
            thumb_path = str(temp_dir / "post_video_thumb.jpg")
            _sp.run(["ffmpeg", "-i", video_path, "-vf", "select=eq(n\\,0)", "-frames:v", "1",
                     thumb_path, "-y"], capture_output=True, timeout=10)

            # Upload thumbnail
            image_key = None
            if os.path.exists(thumb_path):
                image_key = await self._upload_image_for_key(thumb_path)

            # Upload video
            file_name = os.path.basename(video_path)
            with open(video_path, "rb") as f:
                resp = await self.http.post(
                    "https://open.feishu.cn/open-apis/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": "mp4", "file_name": file_name},
                    files={"file": (file_name, f)},
                )
            data = resp.json()
            file_key = data["data"]["file_key"] if data.get("code") == 0 else None
            return file_key, image_key
        except Exception as e:
            logger.error("Video upload for post failed: %s", e)
            return None, None

    async def _handle_reply_post(self, chat_id: str, title: str, content: list) -> dict:
        """Send a rich text post message with mixed text, images, videos, and links.

        Content elements:
        - {"tag":"text", "text":"..."} — text
        - {"tag":"img", "image_key":"..."} or {"tag":"img", "image_path":"/local/path"} — image (auto-uploads)
        - {"tag":"media", "file_key":"...", "image_key":"..."} or {"tag":"media", "video_path":"/local/path"} — video (auto-uploads)
        - {"tag":"a", "text":"...", "href":"..."} — link
        - {"tag":"at", "user_id":"..."} — @mention
        """
        try:
            token = await self.cards._get_token()
            # Auto-upload local files
            for line in content:
                for elem in line:
                    # Auto-upload images
                    if elem.get("tag") == "img" and "image_path" in elem and "image_key" not in elem:
                        key = await self._upload_image_for_key(elem["image_path"])
                        if key:
                            elem["image_key"] = key
                        elem.pop("image_path", None)
                    # Auto-upload videos
                    if elem.get("tag") == "media" and "video_path" in elem and "file_key" not in elem:
                        file_key, image_key = await self._upload_video_for_keys(elem["video_path"])
                        if file_key:
                            elem["file_key"] = file_key
                        if image_key:
                            elem["image_key"] = image_key
                        elem.pop("video_path", None)
            post_body = {"zh_cn": {"title": title, "content": content}}
            resp = await self.http.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": chat_id,
                    "msg_type": "post",
                    "content": json.dumps(post_body),
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                return {"status": "error", "message": f"Post send failed: {data}"}
            return {"status": "ok"}
        except Exception as e:
            logger.error("Post reply failed: %s", e)
            return {"status": "error", "message": f"Post reply failed: {e}"}

    # ── Video reply ────────────────────────────────────────────────

    async def _handle_reply_video(self, chat_id: str, video_path: str) -> dict:
        """Upload a video and send it as an inline video message with auto-generated thumbnail."""
        import os
        import subprocess as _sp
        if not os.path.exists(video_path):
            return {"status": "error", "message": f"File not found: {video_path}"}
        try:
            token = await self.cards._get_token()
            temp_dir = self.settings.temp_dir

            # Step 1: Extract thumbnail from first frame using ffmpeg
            thumb_path = str(temp_dir / "video_thumb.jpg")
            _sp.run(["ffmpeg", "-i", video_path, "-vf", "select=eq(n\\,0)", "-frames:v", "1",
                     thumb_path, "-y"], capture_output=True, timeout=10)

            # Step 2: Upload thumbnail image to get image_key
            image_key = ""
            if os.path.exists(thumb_path):
                with open(thumb_path, "rb") as f:
                    resp = await self.http.post(
                        "https://open.feishu.cn/open-apis/im/v1/images",
                        headers={"Authorization": f"Bearer {token}"},
                        data={"image_type": "message"},
                        files={"image": ("thumb.jpg", f)},
                    )
                upload_data = resp.json()
                if upload_data.get("code") == 0:
                    image_key = upload_data["data"]["image_key"]

            # Step 3: Upload video file to get file_key
            file_name = os.path.basename(video_path)
            with open(video_path, "rb") as f:
                resp = await self.http.post(
                    "https://open.feishu.cn/open-apis/im/v1/files",
                    headers={"Authorization": f"Bearer {token}"},
                    data={"file_type": "mp4", "file_name": file_name},
                    files={"file": (file_name, f)},
                )
            file_data = resp.json()
            if file_data.get("code") != 0:
                return {"status": "error", "message": f"Video upload failed: {file_data}"}
            file_key = file_data["data"]["file_key"]

            # Step 4: Send media message
            content = {"file_key": file_key}
            if image_key:
                content["image_key"] = image_key
            resp = await self.http.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "receive_id": chat_id,
                    "msg_type": "media",
                    "content": json.dumps(content),
                },
            )
            send_data = resp.json()
            if send_data.get("code") != 0:
                return {"status": "error", "message": f"Video send failed: {send_data}"}
            return {"status": "ok", "file_key": file_key}
        except Exception as e:
            logger.error("Video reply failed: %s", e)
            return {"status": "error", "message": f"Video reply failed: {e}"}

    # ── Audio reply ────────────────────────────────────────────────

    async def _handle_reply_audio(self, chat_id: str, text: str) -> dict:
        """Convert text to speech via ElevenLabs and send as audio message."""
        if not self.settings.elevenlabs_api_key or not self.settings.elevenlabs_voice_id:
            return {"status": "error", "message": "ElevenLabs not configured (need ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)"}
        try:
            mp3_path = await text_to_speech(
                self.http,
                self.settings.elevenlabs_api_key,
                self.settings.elevenlabs_voice_id,
                text,
                self.settings.temp_dir,
            )
            return await self.cards.upload_and_send_audio(chat_id, str(mp3_path))
        except Exception as e:
            logger.error("TTS failed: %s", e)
            return {"status": "error", "message": f"TTS failed: {e}"}

    # ── Media download ───────────────────────────────────────────
    # Reuses the CardManager's httpx client and token cache

    async def _download_media(self, content_json: str, message_type: str, message_id: str = "") -> str:
        """Download media and return a description with the local file path."""
        try:
            data = json.loads(content_json)
            token = await self.cards._get_token()
            temp_dir = self.settings.temp_dir
            # Use message_id from parameter (meta), fallback to content JSON
            msg_id = message_id or data.get("message_id", "")

            if message_type == "image":
                path = await download_image(
                    self.http, token, msg_id, data["image_key"], temp_dir
                )
                return f"[Image downloaded to {path}]"
            elif message_type == "audio":
                path = await download_audio(
                    self.http, token, msg_id, data["file_key"], temp_dir
                )
                return f"[Audio file downloaded to {path}]"
            elif message_type == "media":
                file_name = data.get("file_name", "video.mp4")
                path = await download_file(
                    self.http, token, msg_id, data["file_key"],
                    file_name, temp_dir
                )
                return f"[Video downloaded to {path}]"
            elif message_type == "file":
                path = await download_file(
                    self.http, token, msg_id, data["file_key"],
                    data.get("file_name", ""), temp_dir
                )
                return f"[File downloaded to {path}]"
        except Exception as e:
            logger.error("Media download failed: %s", e)
            return f"[Media download failed: {e}]"
        return content_json

    # ── Scheduled task watcher ────────────────────────────────────

    async def _watch_scheduled_tasks(self):
        """Poll for smart task files and inject them as channel notifications."""
        scheduled_dir = SCHEDULED_DIR
        scheduled_dir.mkdir(parents=True, exist_ok=True)

        while True:
            await asyncio.sleep(5)  # check every 5 seconds
            try:
                for task_file in sorted(scheduled_dir.glob("*.json")):
                    try:
                        task = json.loads(task_file.read_text())
                        chat_id = task["chat_id"]
                        prompt = task["prompt"]
                        task_id = task.get("task_id", "unknown")

                        logger.info("Processing scheduled task %s: %s", task_id, prompt[:50])

                        # Create a request_id and card for this task
                        request_id = str(uuid.uuid4())
                        meta = {
                            "user_id": "system_scheduler",
                            "chat_id": chat_id,
                            "sender_name": "scheduled_task",
                            "message_type": "text",
                            "request_id": request_id,
                            "message_id": "",
                            "scheduled_task_id": task_id,
                        }

                        # Pre-create card
                        await self.cards.create_card(request_id, chat_id, "")

                        # Inject as channel notification
                        content = f"[Scheduled task] {prompt}"
                        await self._send_channel_notification(content, meta)

                        # Remove the task file
                        task_file.unlink()
                        logger.info("Scheduled task %s dispatched", task_id)

                    except Exception as e:
                        logger.error("Failed to process task file %s: %s", task_file, e)
                        # Move bad files out of the way
                        task_file.rename(task_file.with_suffix(".error"))
            except Exception as e:
                logger.error("Scheduled task watcher error: %s", e)

    # ── Periodic tasks ───────────────────────────────────────────

    async def _periodic_cleanup(self):
        """Run cleanup tasks every 30 minutes."""
        while True:
            await asyncio.sleep(1800)
            try:
                cleanup_old_files(self.settings.temp_dir, self.settings.temp_file_max_age_hours)
                await self.cards.cleanup_stale_cards()
            except Exception as e:
                logger.error("Cleanup error: %s", e)

    # ── Heartbeat: proactive messaging ────────────────────────────

    async def _send_direct_message(self, chat_id: str, text: str) -> None:
        """Send a text message directly to a Feishu chat (for heartbeat)."""
        import json as _json
        try:
            token = await self.cards._get_token()
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
            resp = await self.http.post(
                "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                headers=headers,
                json={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": _json.dumps({"text": text}),
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                logger.warning("Heartbeat send failed: %s", data)
        except Exception as e:
            logger.error("Heartbeat send error: %s", e)

    # ── Main ─────────────────────────────────────────────────────

    async def run(self):
        """Start the channel: Feishu listener + MCP stdio server."""
        loop = asyncio.get_running_loop()

        # Start Feishu WebSocket in background thread
        self.listener.start(loop)

        # Start periodic cleanup
        asyncio.create_task(self._periodic_cleanup())

        # Start scheduled task watcher
        asyncio.create_task(self._watch_scheduled_tasks())

        # Start heartbeat (proactive messaging when user is inactive)
        configure_inactivity(self.settings.heartbeat_inactivity_minutes)
        asyncio.create_task(heartbeat_loop(
            send_fn=self._send_direct_message,
            model=self.settings.heartbeat_model,
        ))

        # Connect MCP server to Claude Code via stdio
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            # Capture write_stream so we can send notifications from Feishu callbacks
            self._write_stream = write_stream
            init_options = self.server.create_initialization_options(
                experimental_capabilities={"claude/channel": {}}
            )
            await self.server.run(read_stream, write_stream, init_options)


def main():
    """Entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler("/tmp/feishu-channel.log"),
        ],
    )
    channel = FeishuChannel()
    asyncio.run(channel.run())


if __name__ == "__main__":
    main()
