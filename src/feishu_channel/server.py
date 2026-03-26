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
        description="Update the Feishu card with your current status and description. Call this to show the user what you're doing. Auto-creates card if needed.",
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
    types.Tool(
        name="create_doc",
        description="Create a Feishu cloud document with title and content blocks. Returns the document URL. Content blocks: heading1/heading2/heading3, text, bullet, ordered, code, quote.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Document title"},
                "content": {"type": "array", "description": "Array of content blocks: {\"type\":\"heading1\",\"text\":\"...\"}, {\"type\":\"text\",\"text\":\"...\"}, {\"type\":\"bullet\",\"text\":\"...\"}, {\"type\":\"code\",\"text\":\"...\",\"language\":\"python\"}", "items": {"type": "object"}},
                "chat_id": {"type": "string", "description": "Optional: send document link to this chat after creation", "default": ""},
            },
            "required": ["title", "content"],
        },
    ),
    types.Tool(
        name="create_bitable",
        description="Create a Feishu Bitable (多维表格) with custom fields, data, and views. Field types: text, number, single_select, multi_select, date, checkbox, created_time, updated_time. View types: grid, kanban, gallery, gantt, form. NOTE: kanban views require manual group field config in Feishu UI (API limitation). Sends plain link to chat for Feishu preview card.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Bitable title"},
                "fields": {"type": "array", "description": "Field definitions: {\"name\":\"字段名\",\"type\":\"text|number|single_select|multi_select|date|checkbox|created_time|updated_time\",\"options\":[\"选项1\",\"选项2\"]}", "items": {"type": "object"}},
                "records": {"type": "array", "description": "Array of records. Each record maps field names to values. Single select: string, multi select: [strings], date: millisecond timestamp, checkbox: boolean.", "items": {"type": "object"}},
                "views": {"type": "array", "description": "Additional views to create: {\"name\":\"视图名\",\"type\":\"kanban|gallery|gantt|form\"}. Grid view is created by default.", "items": {"type": "object"}, "default": []},
                "chat_id": {"type": "string", "description": "Optional: send bitable link to this chat after creation", "default": ""},
            },
            "required": ["title"],
        },
    ),
    types.Tool(
        name="update_profile",
        description="Update a user's profile for a specific chat context. Profiles are short markdown notes that help tailor responses per user per chat. Call this when you learn something new about a user. Keep profiles concise — they are injected into every message.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "The chat_id (group or p2p)"},
                "user_id": {"type": "string", "description": "The user's open_id (from message meta)"},
                "profile": {"type": "string", "description": "Complete profile in markdown. Keep SHORT (under 500 chars). Include: name, key traits, preferences, relationship context, communication style notes."},
            },
            "required": ["chat_id", "user_id", "profile"],
        },
    ),
    types.Tool(
        name="read_messages",
        description="Read recent messages from a Feishu chat. Returns message history with sender, content, and timestamps. Use for understanding context, summarizing discussions, or finding specific info.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat ID to read messages from"},
                "count": {"type": "integer", "description": "Number of messages to retrieve (max 50)", "default": 10},
            },
            "required": ["chat_id"],
        },
    ),
    types.Tool(
        name="send_reaction",
        description="Send an emoji reaction to a message in Feishu. Types: THUMBSUP, HEART, LAUGH, SURPRISED, CRY, OK, FIRE, CLAP, PARTY, MUSCLE, FINGERHEART",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Message ID to react to"},
                "emoji": {"type": "string", "description": "Emoji type e.g. THUMBSUP, HEART, LAUGH"},
            },
            "required": ["message_id", "emoji"],
        },
    ),
    types.Tool(
        name="bitable_records",
        description="CRUD operations on Feishu Bitable (多维表格) records. Actions: list (read records with optional filter), create (add records), update (modify records), delete (remove records).",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "update", "delete"], "description": "Operation to perform"},
                "app_token": {"type": "string", "description": "Bitable app token (from URL: feishu.cn/base/{app_token})"},
                "table_id": {"type": "string", "description": "Table ID within the bitable"},
                "records": {"type": "array", "description": "For create: [{fields: {name: val}}]. For update: [{record_id: id, fields: {name: val}}]. For delete: [record_id, ...].", "items": {"type": "object"}, "default": []},
                "filter": {"type": "string", "description": "For list: filter string e.g. 'AND(CurrentValue.[Status]=\"Done\")'", "default": ""},
                "page_size": {"type": "integer", "description": "For list: records per page (max 500)", "default": 20},
            },
            "required": ["action", "app_token", "table_id"],
        },
    ),
    types.Tool(
        name="manage_task",
        description="Manage Feishu Tasks (飞书任务) via v1 API. Actions: create (new task), list (bot-created tasks), update (modify task), complete (mark done). Note: list only shows tasks created by the bot, not all user tasks.",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "update", "complete"], "description": "Operation to perform"},
                "summary": {"type": "string", "description": "For create/update: task title/summary", "default": ""},
                "description": {"type": "string", "description": "For create/update: task description", "default": ""},
                "due": {"type": "string", "description": "For create/update: due date as timestamp (seconds) or empty", "default": ""},
                "task_id": {"type": "string", "description": "For update/complete: task ID", "default": ""},
                "page_size": {"type": "integer", "description": "For list: number of tasks", "default": 20},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="search_docs",
        description="Search all Feishu cloud documents (云文档) by keyword. Covers docs, sheets, bitables — everything in your cloud drive. Does NOT search wiki knowledge bases (requires user OAuth). Returns titles, URLs, and document types.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search keyword (max 50 chars)"},
            },
            "required": ["query"],
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

        # User name cache: open_id -> display name
        self._user_names: dict[str, str] = {}

        # Debounce: merge rapid messages from the same chat into one card
        # (debounce removed — deferred card creation handles rapid messages)

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
            elif name == "create_doc":
                result = await channel._handle_create_doc(
                    arguments["title"], arguments.get("content", []), arguments.get("chat_id", ""))
            elif name == "create_bitable":
                result = await channel._handle_create_bitable(
                    arguments["title"], arguments.get("fields", []), arguments.get("records", []),
                    arguments.get("views", []), arguments.get("chat_id", ""))
            elif name == "update_profile":
                result = channel._handle_update_profile(
                    arguments["chat_id"], arguments["user_id"], arguments["profile"])
            elif name == "search_docs":
                result = await channel._handle_search_docs(
                    arguments["query"], arguments.get("space_id", ""))
            elif name == "manage_task":
                result = await channel._handle_manage_task(
                    arguments["action"], arguments.get("summary", ""), arguments.get("description", ""),
                    arguments.get("due", ""), arguments.get("task_id", ""), arguments.get("page_size", 20))
            elif name == "bitable_records":
                result = await channel._handle_bitable_records(
                    arguments["action"], arguments["app_token"], arguments["table_id"],
                    arguments.get("records", []), arguments.get("filter", ""), arguments.get("page_size", 20))
            elif name == "read_messages":
                result = await channel._handle_read_messages(
                    arguments["chat_id"], arguments.get("count", 10))
            elif name == "send_reaction":
                result = await channel._handle_send_reaction(
                    arguments["message_id"], arguments["emoji"])
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

    async def _resolve_user_name(self, user_id: str, chat_id: str) -> str:
        """Resolve a user's display name, with caching. Falls back to user_id."""
        if user_id in self._user_names:
            return self._user_names[user_id]
        try:
            token = await self.cards._get_token()
            # Try chat members API (works for external users too)
            resp = await self.http.get(
                f"https://open.feishu.cn/open-apis/im/v1/chats/{chat_id}/members?member_id_type=open_id",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = resp.json()
            if data.get("code") == 0:
                for m in data["data"].get("items", []):
                    mid = m.get("member_id", "")
                    name = m.get("name", "")
                    if mid and name:
                        self._user_names[mid] = name
        except Exception as e:
            logger.debug("Failed to resolve user names: %s", e)
        return self._user_names.get(user_id, user_id)

    async def _on_feishu_message(self, content: str, request_id: str, meta: dict) -> None:
        """Called when a Feishu message arrives. Sends notification directly to Claude."""
        mark_activity()
        chat_id = meta["chat_id"]
        message_id = meta.get("message_id", "")

        # Resolve sender name
        user_id = meta.get("user_id", "")
        if user_id:
            meta["sender_name"] = await self._resolve_user_name(user_id, chat_id)

        # Fetch replied-to message content if this is a reply
        parent_id = meta.get("parent_id", "")
        if parent_id:
            try:
                token = await self.cards._get_token()
                resp = await self.http.get(
                    f"https://open.feishu.cn/open-apis/im/v1/messages/{parent_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                data = resp.json()
                if data.get("code") == 0:
                    items = data.get("data", {}).get("items", [])
                    if items:
                        item = items[0]
                        parent_body = item.get("body", {}).get("content", "")
                        parent_type = item.get("msg_type", "text")
                        try:
                            parent_json = json.loads(parent_body)
                            if parent_type == "text":
                                meta["reply_to_content"] = parent_json.get("text", "")
                            elif parent_type == "interactive":
                                # Card message — extract text from elements
                                texts = []
                                elements = parent_json.get("elements", [])
                                for row in elements:
                                    if isinstance(row, list):
                                        for el in row:
                                            if isinstance(el, dict) and el.get("tag") == "text" and el.get("text"):
                                                texts.append(el["text"])
                                    elif isinstance(row, dict) and row.get("tag") == "text" and row.get("text"):
                                        texts.append(row["text"])
                                title = parent_json.get("title", "")
                                if title:
                                    texts.insert(0, title)
                                meta["reply_to_content"] = " ".join(texts) if texts else parent_body
                            elif parent_type == "post":
                                # Rich text — extract text tags
                                texts = []
                                title = parent_json.get("title", "")
                                if title:
                                    texts.append(title)
                                for row in parent_json.get("content", []):
                                    if isinstance(row, list):
                                        for el in row:
                                            if isinstance(el, dict) and el.get("text"):
                                                texts.append(el["text"])
                                meta["reply_to_content"] = " ".join(texts) if texts else parent_body
                            else:
                                meta["reply_to_content"] = parent_body
                        except (json.JSONDecodeError, TypeError):
                            meta["reply_to_content"] = parent_body
            except Exception as e:
                logger.debug("Failed to fetch parent message %s: %s", parent_id, e)

        # Handle media downloads
        message_type = meta["message_type"]
        if message_type in ("image", "audio", "file", "media"):
            content = await self._download_media(content, message_type, message_id)
        elif message_type == "post":
            content = await self._process_post_content(content, message_id)

        # Defer card creation — card appears only when Claude calls update_status/reply
        self.cards.register_pending(request_id, chat_id, message_id)

        # Inject user profile into meta
        if user_id:
            profile = self._load_profile(chat_id, user_id)
            meta["user_profile"] = profile  # always inject, even if empty

        # Send notification to Claude
        await self._send_channel_notification(content, meta)

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

    # ── User profiles ──────────────────────────────────────────

    _PROFILES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "profiles")

    _PROFILE_MAX_CHARS = 500

    def _handle_update_profile(self, chat_id: str, user_id: str, profile: str) -> dict:
        """Write/update a user profile markdown file. Rejects if too long."""
        if len(profile) > self._PROFILE_MAX_CHARS:
            return {"status": "error", "message": f"Profile size exceeded ({len(profile)}/{self._PROFILE_MAX_CHARS} chars). Need to summarize."}
        os.makedirs(self._PROFILES_DIR, exist_ok=True)
        filename = f"{chat_id}_{user_id}.md"
        filepath = os.path.join(self._PROFILES_DIR, filename)
        with open(filepath, "w") as f:
            f.write(profile)
        return {"status": "ok", "file": filename}

    @staticmethod
    def _load_profile(chat_id: str, user_id: str) -> str:
        """Load a user's profile for a given chat. Returns empty string if none."""
        profiles_dir = os.path.join(os.path.dirname(__file__), "..", "..", "profiles")
        filepath = os.path.join(profiles_dir, f"{chat_id}_{user_id}.md")
        if os.path.isfile(filepath):
            with open(filepath) as f:
                return f.read().strip()
        return ""

    # ── Wiki search ───────────────────────────────────────────

    async def _handle_search_docs(self, query: str, space_id: str = "") -> dict:
        """Search Feishu docs and knowledge base. Uses suite/docs-api/search which works with tenant_access_token."""
        try:
            token = await self.cards._get_token()
            body: dict = {"search_key": query[:50], "count": 20, "offset": 0}

            resp = await self.http.post(
                "https://open.feishu.cn/open-apis/suite/docs-api/search/object",
                headers={"Authorization": f"Bearer {token}"},
                json=body,
            )
            data = resp.json()
            if data.get("code") != 0:
                return {"status": "error", "message": f"Wiki search failed: {data.get('msg', 'unknown error')}"}

            items = data.get("data", {}).get("docs_entities", [])
            total = data.get("data", {}).get("total", 0)
            results = []
            for item in items:
                doc_token = item.get("docs_token", "")
                doc_type = item.get("docs_type", "doc")
                # Construct URL based on doc type
                if doc_type == "bitable":
                    url = f"https://feishu.cn/base/{doc_token}"
                elif doc_type == "sheet":
                    url = f"https://feishu.cn/sheets/{doc_token}"
                else:
                    url = f"https://feishu.cn/docx/{doc_token}"
                results.append({
                    "title": item.get("title", ""),
                    "url": url,
                    "doc_token": doc_token,
                    "doc_type": doc_type,
                })

            return {"status": "ok", "count": len(results), "total": total, "results": results}
        except Exception as e:
            return {"status": "error", "message": f"Wiki search error: {e}"}

    # ── Task management ────────────────────────────────────────

    async def _handle_manage_task(self, action: str, summary: str = "", description: str = "",
                                   due: str = "", task_id: str = "", page_size: int = 20) -> dict:
        """Manage Feishu Tasks."""
        try:
            token = await self.cards._get_token()
            headers = {"Authorization": f"Bearer {token}"}
            base_url = "https://open.feishu.cn/open-apis/task/v1/tasks"

            if action == "create":
                if not summary:
                    return {"status": "error", "message": "summary required"}
                body: dict = {"summary": summary}
                if description:
                    body["description"] = description
                if due:
                    body["due"] = {"time": due, "is_all_day": False}
                resp = await self.http.post(base_url, headers=headers, json=body)
                data = resp.json()
                if data.get("code") == 0:
                    task = data.get("data", {}).get("task", {})
                    return {"status": "ok", "task_id": task.get("id", ""), "summary": task.get("summary", "")}
                return {"status": "error", "message": data.get("msg", "")}

            elif action == "list":
                resp = await self.http.get(base_url, headers=headers, params={"page_size": min(page_size, 50)})
                data = resp.json()
                if data.get("code") == 0:
                    items = data.get("data", {}).get("items", [])
                    tasks = []
                    for t in items:
                        tasks.append({
                            "task_id": t.get("id", ""),
                            "summary": t.get("summary", ""),
                            "description": t.get("description", ""),
                            "completed": t.get("complete_time", "0") != "0",
                            "due": t.get("due", {}).get("time", "") if t.get("due") else "",
                        })
                    return {"status": "ok", "count": len(tasks), "tasks": tasks}
                return {"status": "error", "message": data.get("msg", "")}

            elif action == "update":
                if not task_id:
                    return {"status": "error", "message": "task_id required"}
                body = {}
                if summary:
                    body["summary"] = summary
                if description:
                    body["description"] = description
                if due:
                    body["due"] = {"timestamp": due, "is_all_day": False}
                resp = await self.http.patch(f"{base_url}/{task_id}", headers=headers, json=body)
                data = resp.json()
                if data.get("code") == 0:
                    return {"status": "ok", "task_id": task_id}
                return {"status": "error", "message": data.get("msg", "")}

            elif action == "complete":
                if not task_id:
                    return {"status": "error", "message": "task_id required"}
                resp = await self.http.post(f"{base_url}/{task_id}/complete", headers=headers, json={})
                data = resp.json()
                if data.get("code") == 0:
                    return {"status": "ok", "task_id": task_id, "completed": True}
                return {"status": "error", "message": data.get("msg", "")}

            else:
                return {"status": "error", "message": f"Unknown action: {action}"}

        except Exception as e:
            return {"status": "error", "message": f"Task error: {e}"}

    # ── Bitable CRUD ───────────────────────────────────────────

    async def _handle_bitable_records(self, action: str, app_token: str, table_id: str,
                                       records: list = None, filter_str: str = "", page_size: int = 20) -> dict:
        """CRUD operations on Bitable records."""
        try:
            token = await self.cards._get_token()
            base_url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records"
            headers = {"Authorization": f"Bearer {token}"}

            if action == "list":
                params = {"page_size": min(page_size, 500)}
                if filter_str:
                    params["filter"] = filter_str
                resp = await self.http.get(base_url, headers=headers, params=params)
                data = resp.json()
                if data.get("code") != 0:
                    return {"status": "error", "message": data.get("msg", "")}
                items = data.get("data", {}).get("items", [])
                return {
                    "status": "ok",
                    "total": data.get("data", {}).get("total", 0),
                    "records": [{"record_id": r.get("record_id"), "fields": r.get("fields", {})} for r in items]
                }

            elif action == "create":
                if not records:
                    return {"status": "error", "message": "records required for create"}
                created = []
                for rec in records:
                    fields = rec if not isinstance(rec, dict) or "fields" not in rec else rec["fields"]
                    resp = await self.http.post(base_url, headers=headers, json={"fields": fields})
                    data = resp.json()
                    if data.get("code") == 0:
                        r = data.get("data", {}).get("record", {})
                        created.append({"record_id": r.get("record_id"), "fields": r.get("fields", {})})
                    else:
                        created.append({"error": data.get("msg", "")})
                return {"status": "ok", "created": len([c for c in created if "record_id" in c]), "records": created}

            elif action == "update":
                if not records:
                    return {"status": "error", "message": "records required for update (each needs record_id + fields)"}
                updated = []
                for rec in records:
                    rid = rec.get("record_id", "")
                    fields = rec.get("fields", {})
                    if not rid:
                        updated.append({"error": "missing record_id"})
                        continue
                    resp = await self.http.put(f"{base_url}/{rid}", headers=headers, json={"fields": fields})
                    data = resp.json()
                    if data.get("code") == 0:
                        r = data.get("data", {}).get("record", {})
                        updated.append({"record_id": r.get("record_id"), "fields": r.get("fields", {})})
                    else:
                        updated.append({"record_id": rid, "error": data.get("msg", "")})
                return {"status": "ok", "updated": len([u for u in updated if "error" not in u]), "records": updated}

            elif action == "delete":
                if not records:
                    return {"status": "error", "message": "records required for delete (list of record_ids)"}
                # records can be list of strings or list of dicts with record_id
                ids = [r if isinstance(r, str) else r.get("record_id", "") for r in records]
                ids = [i for i in ids if i]
                if not ids:
                    return {"status": "error", "message": "no valid record_ids"}
                resp = await self.http.post(f"{base_url}/batch_delete", headers=headers, json={"records": ids})
                data = resp.json()
                if data.get("code") == 0:
                    return {"status": "ok", "deleted": len(ids)}
                return {"status": "error", "message": data.get("msg", "")}

            else:
                return {"status": "error", "message": f"Unknown action: {action}"}

        except Exception as e:
            return {"status": "error", "message": f"Bitable error: {e}"}

    # ── Message history & reactions ─────────────────────────────

    async def _handle_read_messages(self, chat_id: str, count: int = 10) -> dict:
        """Read recent messages from a Feishu chat."""
        try:
            count = min(count, 50)
            token = await self.cards._get_token()
            resp = await self.http.get(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "container_id_type": "chat",
                    "container_id": chat_id,
                    "sort_type": "ByCreateTimeDesc",
                    "page_size": count,
                },
            )
            data = resp.json()
            if data.get("code") != 0:
                return {"status": "error", "message": f"Read messages failed: {data.get('msg', '')}"}

            items = data.get("data", {}).get("items", [])
            messages = []
            for item in items:
                sender = item.get("sender", {})
                body = item.get("body", {}).get("content", "")
                msg_type = item.get("msg_type", "text")

                # Extract text from content
                text = ""
                try:
                    content_json = json.loads(body)
                    if msg_type == "text":
                        text = content_json.get("text", "")
                    elif msg_type == "interactive":
                        # Card — extract text elements
                        texts = []
                        for row in content_json.get("elements", []):
                            if isinstance(row, list):
                                for el in row:
                                    if isinstance(el, dict) and el.get("tag") == "text" and el.get("text"):
                                        texts.append(el["text"])
                        title = content_json.get("title", "")
                        text = (title + " " + " ".join(texts)).strip() if texts or title else "[card]"
                    elif msg_type == "post":
                        texts = []
                        for row in content_json.get("content", []):
                            if isinstance(row, list):
                                for el in row:
                                    if isinstance(el, dict) and el.get("text"):
                                        texts.append(el["text"])
                        text = " ".join(texts) if texts else "[post]"
                    else:
                        text = f"[{msg_type}]"
                except (json.JSONDecodeError, TypeError):
                    text = body[:200] if body else f"[{msg_type}]"

                # Skip bot's own messages that are just cards with no meaningful text
                if sender.get("sender_type") == "app" and text in ("[card]", "[interactive]"):
                    continue

                messages.append({
                    "sender_id": sender.get("id", ""),
                    "sender_type": sender.get("sender_type", ""),
                    "msg_type": msg_type,
                    "text": text[:500],
                    "message_id": item.get("message_id", ""),
                    "create_time": item.get("create_time", ""),
                })

            return {"status": "ok", "count": len(messages), "messages": messages}
        except Exception as e:
            return {"status": "error", "message": f"Read messages error: {e}"}

    async def _handle_send_reaction(self, message_id: str, emoji: str) -> dict:
        """Send an emoji reaction to a message."""
        try:
            token = await self.cards._get_token()
            resp = await self.http.post(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions",
                headers={"Authorization": f"Bearer {token}"},
                json={"reaction_type": {"emoji_type": emoji}},
            )
            data = resp.json()
            if data.get("code") == 0:
                return {"status": "ok"}
            return {"status": "error", "message": f"Send reaction failed: {data.get('msg', '')}"}
        except Exception as e:
            return {"status": "error", "message": f"Send reaction error: {e}"}

    # ── Feishu Doc / Spreadsheet creation ───────────────────────

    async def _handle_create_doc(self, title: str, content: list, chat_id: str = "") -> dict:
        """Create a Feishu cloud document with structured content."""
        try:
            token = await self.cards._get_token()
            # Step 1: Create document
            resp = await self.http.post(
                "https://open.feishu.cn/open-apis/docx/v1/documents",
                headers={"Authorization": f"Bearer {token}"},
                json={"title": title},
            )
            data = resp.json()
            if data.get("code") != 0:
                return {"status": "error", "message": f"Doc creation failed: {data}"}
            doc_id = data["data"]["document"]["document_id"]

            # Step 2: Add content blocks
            if content:
                block_type_map = {"heading1": 3, "heading2": 4, "heading3": 5, "text": 2, "bullet": 12, "ordered": 13, "code": 14, "quote": 15}
                children = []
                for block in content:
                    bt = block.get("type", "text")
                    text = block.get("text", "")
                    block_type = block_type_map.get(bt, 2)
                    block_key = bt if bt in block_type_map else "text"
                    if block_key in ("heading1", "heading2", "heading3"):
                        children.append({"block_type": block_type, block_key: {"elements": [{"text_run": {"content": text}}]}})
                    elif block_key == "code":
                        lang = block.get("language", "plain_text")
                        children.append({"block_type": block_type, "code": {"elements": [{"text_run": {"content": text}}], "language": lang}})
                    else:
                        children.append({"block_type": block_type, block_key: {"elements": [{"text_run": {"content": text}}]}})

                await self.http.post(
                    f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}/blocks/{doc_id}/children",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"children": children, "index": 0},
                )

            # Step 3: Get the URL from the API response (or construct fallback)
            try:
                doc_resp = await self.http.get(
                    f"https://open.feishu.cn/open-apis/docx/v1/documents/{doc_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                doc_data = doc_resp.json()
                url = doc_data.get("data", {}).get("document", {}).get("url", "")
                if not url:
                    url = f"https://feishu.cn/docx/{doc_id}"
            except Exception:
                url = f"https://feishu.cn/docx/{doc_id}"

            # Step 4: Optionally send plain link to chat (renders as doc preview card)
            if chat_id:
                await self.http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": url}),
                    },
                )

            return {"status": "ok", "document_id": doc_id, "url": url}
        except Exception as e:
            logger.error("Create doc failed: %s", e)
            return {"status": "error", "message": f"Create doc failed: {e}"}

    async def _handle_create_bitable(self, title: str, fields: list = None, records: list = None,
                                      views: list = None, chat_id: str = "") -> dict:
        """Create a Feishu Bitable (多维表格) with custom fields, data, and views."""
        try:
            token = await self.cards._get_token()
            field_type_map = {
                "text": 1, "number": 2, "single_select": 3, "multi_select": 4,
                "date": 5, "checkbox": 7, "created_time": 1002, "updated_time": 1003,
            }

            # Step 1: Create bitable
            resp = await self.http.post(
                "https://open.feishu.cn/open-apis/bitable/v1/apps",
                headers={"Authorization": f"Bearer {token}"},
                json={"name": title},
            )
            data = resp.json()
            if data.get("code") != 0:
                return {"status": "error", "message": f"Bitable creation failed: {data}"}
            app_token = data["data"]["app"]["app_token"]
            url = data["data"]["app"]["url"]

            # Get default table
            resp = await self.http.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables",
                headers={"Authorization": f"Bearer {token}"},
            )
            items = resp.json().get("data", {}).get("items", [])
            if not items:
                return {"status": "error", "message": "Bitable created but no default table found"}
            table_id = items[0]["table_id"]

            # Delete default fields (except the first one which we'll repurpose)
            resp = await self.http.get(
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                headers={"Authorization": f"Bearer {token}"},
            )
            default_fields = resp.json()["data"]["items"]
            for f in default_fields[1:]:
                await self.http.delete(
                    f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{f['field_id']}",
                    headers={"Authorization": f"Bearer {token}"},
                )

            # Step 2: Add custom fields
            if fields:
                # Rename first default field to the first custom field
                first_field = fields[0]
                ft = field_type_map.get(first_field.get("type", "text"), 1)
                update_json = {"field_name": first_field["name"], "type": ft}
                if first_field.get("options"):
                    update_json["property"] = {"options": [{"name": o} for o in first_field["options"]]}
                await self.http.put(
                    f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields/{default_fields[0]['field_id']}",
                    headers={"Authorization": f"Bearer {token}"},
                    json=update_json,
                )

                # Add remaining fields
                for f in fields[1:]:
                    ft = field_type_map.get(f.get("type", "text"), 1)
                    field_json = {"field_name": f["name"], "type": ft}
                    if f.get("options"):
                        field_json["property"] = {"options": [{"name": o} for o in f["options"]]}
                    await self.http.post(
                        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                        headers={"Authorization": f"Bearer {token}"},
                        json=field_json,
                    )

            # Step 3: Add records (one by one — batch_create has data loss issues)
            if records:
                for r in records:
                    await self.http.post(
                        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/records",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"fields": r},
                    )

            # Step 4: Add views (kanban group field must be set manually in Feishu UI)
            if views:
                view_type_map = {"kanban": "kanban", "gallery": "gallery", "gantt": "gantt", "form": "form", "grid": "grid"}
                for v in views:
                    vt = view_type_map.get(v.get("type", "grid"), "grid")
                    await self.http.post(
                        f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}/views",
                        headers={"Authorization": f"Bearer {token}"},
                        json={"view_name": v.get("name", vt), "view_type": vt},
                    )

            # Step 5: Send plain link to chat
            if chat_id:
                await self.http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": url}),
                    },
                )

            return {"status": "ok", "app_token": app_token, "url": url}
        except Exception as e:
            logger.error("Create bitable failed: %s", e)
            return {"status": "error", "message": f"Create bitable failed: {e}"}

    # ── Post (rich text) inbound processing ─────────────────────

    async def _process_post_content(self, content_json: str, message_id: str) -> str:
        """Parse post (rich text) content, download embedded media, and return readable text.

        Converts the post JSON into a sequential text representation where images and
        videos are downloaded and replaced with local file paths, preserving order.
        """
        try:
            data = json.loads(content_json)
            post = data.get("content", [])
            title = data.get("title", "")
            token = await self.cards._get_token()
            temp_dir = self.settings.temp_dir

            lines = []
            if title:
                lines.append(f"[Title: {title}]")

            media_idx = 0
            for line_elements in post:
                line_parts = []
                for elem in line_elements:
                    tag = elem.get("tag", "")

                    if tag == "text":
                        line_parts.append(elem.get("text", ""))

                    elif tag == "a":
                        text = elem.get("text", "")
                        href = elem.get("href", "")
                        line_parts.append(f"{text}({href})")

                    elif tag == "at":
                        line_parts.append(f"@{elem.get('user_name', elem.get('user_id', ''))}")

                    elif tag == "img":
                        image_key = elem.get("image_key", "")
                        if image_key and message_id:
                            try:
                                path = await download_image(
                                    self.http, token, message_id, image_key, temp_dir
                                )
                                line_parts.append(f"[Image downloaded to {path}]")
                            except Exception as e:
                                line_parts.append(f"[Image: {image_key} (download failed: {e})]")
                        else:
                            line_parts.append(f"[Image: {image_key}]")

                    elif tag == "media":
                        file_key = elem.get("file_key", "")
                        file_name = elem.get("file_name", f"media_{media_idx}.mp4")
                        if file_key and message_id:
                            try:
                                path = await download_file(
                                    self.http, token, message_id, file_key, file_name, temp_dir
                                )
                                line_parts.append(f"[Video downloaded to {path}]")
                            except Exception as e:
                                line_parts.append(f"[Video: {file_key} (download failed: {e})]")
                        else:
                            line_parts.append(f"[Video: {file_key}]")
                        media_idx += 1

                    elif tag == "emotion":
                        line_parts.append(elem.get("emoji_type", ""))

                if line_parts:
                    lines.append("".join(line_parts))

            return "\n".join(lines)
        except Exception as e:
            logger.error("Post content processing failed: %s", e)
            return content_json

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
