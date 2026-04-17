"""Xiaobai MCP server — thin layer over channels + tools.

Responsibilities:

* Load :class:`Settings` and instantiate :class:`FeishuChannel` +
  :class:`WeChatChannel`, registering them with :class:`ChannelRegistry`.
* Declare the 22 MCP tools (inputSchema preserved from legacy server.py).
* Central dispatcher — resolve aliases + short ids, route each tool call to
  the right handler in ``xiaobai.tools``.
* Ingress handler for inbound messages — fetch replies, download media,
  register short ids + profile injection, then hand to
  :class:`NotificationPipeline`.
* Run :func:`mcp.server.stdio.stdio_server` and bind the write_stream so
  notifications reach Claude Code.

The live bot keeps running on the legacy ``src/feishu_channel/server.py``
until Session 3 flips ``.mcp.json`` and ``pyproject.toml``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import mcp.server.stdio
from mcp import types
from mcp.server.lowlevel import Server

from .channels.feishu.channel import FeishuChannel
from .channels.feishu.media import (
    cleanup_old_files,
    download_file,
    download_image,
    transcribe_audio,
)
from .channels.wechat.channel import WeChatChannel
from .config import Settings
from .core.notifications import NotificationPipeline
from .core.registry import ChannelRegistry
from .tools import (
    cards as tools_cards,
    docs as tools_docs,
    heartbeat as tools_heartbeat,
    media_search as tools_media_search,
    messaging as tools_messaging,
    profile as tools_profile,
    reminders as tools_reminders,
    wechat_login as tools_wechat_login,
)
from .utils.short_ids import ShortIdMap

logger = logging.getLogger(__name__)


# ── Tool inputSchema (preserved from legacy server.py, descriptions trimmed) ──

TOOLS: list[types.Tool] = [
    types.Tool(
        name="reply",
        description=(
            "Send a text message to a chat. Call multiple times for multiple "
            "bubbles. This is the ONLY way users see your response — plain "
            "text is invisible. Supports markdown and @mentions: "
            "<at id=user_id></at> or <at id=all></at>."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat to send to"},
                "text": {"type": "string", "description": "Message text"},
                "reply_to": {"type": "string", "description": "Optional: message_id to reply to (quoted reply)."},
            },
            "required": ["chat_id", "text"],
        },
    ),
    types.Tool(
        name="reply_card",
        description=(
            "Send or update a Feishu interactive card. Use for multi-step "
            "tasks (progress) or structured responses (V2 card JSON). "
            "done=false updates, done=true finalizes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "The request_id from the inbound message"},
                "status": {"type": "string", "description": "Short status in card header (<15 chars)"},
                "text": {"type": "string", "description": "Body text; V2 card JSON allowed when done=true."},
                "done": {"type": "boolean", "description": "false = update, true = finalize", "default": False},
                "emoji": {"type": "string", "description": "Header emoji (done=false only)", "default": "⏳"},
                "template": {"type": "string", "description": "Header color: blue, green, yellow, orange, red, violet, indigo, grey, default", "default": "indigo"},
            },
            "required": ["request_id", "text"],
        },
    ),
    types.Tool(
        name="reply_file",
        description="Send a file to a chat (PDF, Excel, ZIP, etc). File must exist at an absolute local path.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat to send to"},
                "file_path": {"type": "string", "description": "Absolute path to the file"},
            },
            "required": ["chat_id", "file_path"],
        },
    ),
    types.Tool(
        name="reply_image",
        description="Send an image inline (PNG/JPG/GIF). Pair with search_image for contextual photos.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat to send to"},
                "image_path": {"type": "string", "description": "Absolute path to the image"},
            },
            "required": ["chat_id", "image_path"],
        },
    ),
    types.Tool(
        name="reply_video",
        description="Send a video inline with a built-in player (MP4/MOV). Auto-generates thumbnail.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat to send to"},
                "video_path": {"type": "string", "description": "Absolute path to the video"},
            },
            "required": ["chat_id", "video_path"],
        },
    ),
    types.Tool(
        name="reply_post",
        description=(
            "Send a rich message mixing text, images, videos, links. Use when "
            "you need formatted content with inline media. Local image/video "
            "paths are auto-uploaded."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat to send to"},
                "title": {"type": "string", "description": "Post title (optional)", "default": ""},
                "content": {
                    "type": "array",
                    "description": "Array of lines. Each line is an array of elements: {\"tag\":\"text\",\"text\":\"...\"} or {\"tag\":\"img\",\"image_key\":\"...\"} or {\"tag\":\"a\",\"text\":\"...\",\"href\":\"url\"}",
                    "items": {"type": "array", "items": {"type": "object"}},
                },
            },
            "required": ["chat_id", "content"],
        },
    ),
    types.Tool(
        name="reply_audio",
        description="Convert text to a voice message and send (ElevenLabs TTS). Requires API key.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat to send to"},
                "text": {"type": "string", "description": "Text to TTS (max 2000 chars)"},
            },
            "required": ["chat_id", "text"],
        },
    ),
    types.Tool(
        name="create_reminder",
        description=(
            "Schedule a recurring message or smart prompt on cron. smart=false "
            "sends fixed text; smart=true triggers Claude to think fresh. Cron "
            "is UTC (auto-converted to local). Use max_runs=1 for one-shots."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "reminder_id": {"type": "string", "description": "Unique ID"},
                "cron_expression": {"type": "string", "description": "5-field cron"},
                "chat_id": {"type": "string", "description": "Target chat_id"},
                "message": {"type": "string", "description": "Message text or prompt"},
                "smart": {"type": "boolean", "description": "Trigger Claude vs fixed text", "default": False},
                "max_runs": {"type": "integer", "description": "Auto-delete after N runs (0=unlimited)", "default": 0},
            },
            "required": ["reminder_id", "cron_expression", "chat_id", "message"],
        },
    ),
    types.Tool(
        name="list_reminders",
        description="List all active scheduled reminders.",
        inputSchema={"type": "object", "properties": {}},
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
        description=(
            "Create a Feishu Doc (云文档) for long-form content — reports, "
            "guides, notes. Returns shareable URL. Block types: heading1/"
            "heading2/heading3, text, bullet, ordered, code, quote."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Document title"},
                "content": {"type": "array", "description": "Array of blocks: {type, text, language?}", "items": {"type": "object"}},
                "chat_id": {"type": "string", "description": "Optional: broadcast link to chat", "default": ""},
            },
            "required": ["title", "content"],
        },
    ),
    types.Tool(
        name="create_bitable",
        description=(
            "Create a Feishu Bitable — structured database with custom fields, "
            "records, views. Auto-cleans default rows. URL values auto-convert "
            "to {text,link}. Field types: text, number, single_select, "
            "multi_select, date (ms), checkbox, url, phone, user, attachment, "
            "created_time, modified_time."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Bitable title"},
                "fields": {"type": "array", "description": "{name, type, options?}", "items": {"type": "object"}},
                "records": {"type": "array", "description": "Maps field name → value", "items": {"type": "object"}},
                "views": {"type": "array", "description": "{name, type: kanban|gallery|gantt|form}", "items": {"type": "object"}, "default": []},
                "chat_id": {"type": "string", "description": "Optional: broadcast link", "default": ""},
            },
            "required": ["title"],
        },
    ),
    types.Tool(
        name="update_profile",
        description="Save/update a user's profile. Structured fields auto-format into template. Update when you learn something new.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat (group or p2p)"},
                "user_id": {"type": "string", "description": "User's open_id"},
                "name": {"type": "string", "description": "Display name used as alias"},
                "title": {"type": "string", "description": "Role/称呼", "default": ""},
                "real_name": {"type": "string", "description": "Real name", "default": ""},
                "location": {"type": "string", "description": "Location + timezone", "default": ""},
                "phone": {"type": "string", "description": "Phone", "default": ""},
                "notes": {"type": "string", "description": "Free-form notes", "default": ""},
            },
            "required": ["chat_id", "user_id", "name"],
        },
    ),
    types.Tool(
        name="read_messages",
        description=(
            "Fetch recent messages. Feishu → live API; WeChat (@im.wechat) → "
            "local jsonl. Use to catch up on context or summarize discussions."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "Chat ID"},
                "count": {"type": "integer", "description": "Max 50", "default": 10},
            },
            "required": ["chat_id"],
        },
    ),
    types.Tool(
        name="send_reaction",
        description=(
            "React to a message with an emoji. Use instead of 'ok'/'got it'. "
            "THUMBSUP, LAUGH, HEART, Fire, CLAP, FACEPALM, MUSCLE, OK, etc. "
            "176 types, case-sensitive. "
            "https://open.feishu.cn/document/server-docs/im-v1/message-reaction/emojis-introduce"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Message to react to"},
                "emoji": {"type": "string", "description": "Emoji type (e.g. THUMBSUP, HEART)"},
            },
            "required": ["message_id", "emoji"],
        },
    ),
    types.Tool(
        name="bitable_records",
        description=(
            "CRUD on existing Bitable records. Actions: list, create, update "
            "(by record_id), delete. Get app_token from URL: feishu.cn/base/"
            "{app_token}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["list", "create", "update", "delete"]},
                "app_token": {"type": "string", "description": "From feishu.cn/base/{app_token}"},
                "table_id": {"type": "string", "description": "Table ID"},
                "records": {"type": "array", "description": "Shape depends on action", "items": {"type": "object"}, "default": []},
                "filter": {"type": "string", "description": "list: filter string", "default": ""},
                "page_size": {"type": "integer", "description": "list: page size (max 500)", "default": 20},
            },
            "required": ["action", "app_token", "table_id"],
        },
    ),
    types.Tool(
        name="manage_task",
        description=(
            "Create/manage Feishu Tasks (飞书任务). Actions: create, list, "
            "update, complete. Note: v1 API — list only returns bot-created "
            "tasks."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["create", "list", "update", "complete"]},
                "summary": {"type": "string", "default": ""},
                "description": {"type": "string", "default": ""},
                "due": {"type": "string", "description": "Timestamp seconds", "default": ""},
                "task_id": {"type": "string", "default": ""},
                "page_size": {"type": "integer", "default": 20},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="manage_heartbeat",
        description="Manage your heartbeat watchlist. Each chat has its own interval.",
        inputSchema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["add", "remove", "list", "set_interval"]},
                "chat_id": {"type": "string", "default": ""},
                "label": {"type": "string", "default": ""},
                "interval": {"type": "integer", "description": "Per-chat minutes (min 10, default 15)", "default": 0},
            },
            "required": ["action"],
        },
    ),
    types.Tool(
        name="wechat_login_qr",
        description=(
            "Generate a WeChat bot login QR. User scans to link their account. "
            "Multi-account: each scan creates a separate account."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "account_id": {"type": "string", "description": "Account label", "default": "new"},
                "chat_id": {"type": "string", "description": "Optional: chat to send QR to"},
            },
            "required": [],
        },
    ),
    types.Tool(
        name="search_image",
        description=(
            "Search photos (Pexels) or GIFs (Tenor). Use proactively: travel → "
            "scenery, food → dishes, funny → reactions. Returns local_path "
            "ready for reply_image."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "English works best"},
                "type": {"type": "string", "enum": ["photo", "gif"], "default": "photo"},
                "count": {"type": "integer", "description": "1-5", "default": 1},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="search_docs",
        description=(
            "Search all Feishu cloud docs (Docs/Sheets/Bitables/Slides) by "
            "keyword. Use before saying 'I don't know'. Does NOT cover wiki."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword (max 50 chars)"},
            },
            "required": ["query"],
        },
    ),
    types.Tool(
        name="get_user_info",
        description="Get a Feishu user's profile: name, avatar, department. Pass download_avatar=true to get local image path.",
        inputSchema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string", "description": "Feishu open_id (ou_xxx)"},
                "download_avatar": {"type": "boolean", "default": False},
            },
            "required": ["user_id"],
        },
    ),
]


# ── Orchestrator ─────────────────────────────────────────────────


class XiaobaiServer:
    """Holds channels + MCP server + notification pipeline + ingress state."""

    def __init__(self) -> None:
        self.settings = Settings()
        self.settings.temp_dir.mkdir(parents=True, exist_ok=True)

        # Build channels
        self.feishu = FeishuChannel(
            app_id=self.settings.feishu_app_id,
            app_secret=self.settings.feishu_app_secret,
            allowed_user_ids=self.settings.allowed_user_ids,
            temp_dir=self.settings.temp_dir,
            elevenlabs_api_key=self.settings.elevenlabs_api_key,
            elevenlabs_voice_id=self.settings.elevenlabs_voice_id,
            stale_card_timeout_minutes=self.settings.stale_card_timeout_minutes,
        )
        try:
            self.wechat: WeChatChannel | None = WeChatChannel(
                ilink_base_url=self.settings.ilink_base_url,
                ilink_cdn_url=self.settings.ilink_cdn_url,
                state_dir=self.settings.state_dir,
                wechat_temp_dir=self.settings.wechat_temp_dir,
            )
        except Exception as e:
            logger.warning("WeChat channel init failed: %s", e)
            self.wechat = None

        # Registry — WeChat first so its ``@im.wechat`` suffix wins over
        # Feishu's ``oc_/ou_`` prefix check.
        self.registry = ChannelRegistry()
        if self.wechat is not None:
            self.registry.add(self.wechat)
        self.registry.add(self.feishu)

        # State originally on FeishuChannel in server.py:402-453
        self._last_reply_times: dict[str, str] = {}
        self._current_user: dict[str, str] = {}  # chat_id -> user_id
        self._profile_inject_state: dict[str, tuple[int, float]] = {}
        self._short_ids = ShortIdMap()

        # Media dedup (md5 → (path, sender, ts))
        import hashlib as _hashlib
        self._hashlib = _hashlib
        self._media_hashes: dict[str, tuple[str, str, float]] = {}

        # Notification plumbing — pipeline is built in run() once we have the
        # write stream.
        self._write_stream = None
        self._pipeline: NotificationPipeline | None = None

        # MCP server + tool registration
        self.server: Server = Server(
            name="channel",
            version="0.2.0",
            instructions=self.settings.load_instructions(),
        )
        self._register_tools()

    # ── Short-id helpers (thin adapters over ShortIdMap) ─────────

    def _register_short_ids(self, message_id: str, request_id: str) -> tuple[str, str]:
        return self._short_ids.register(message_id, request_id)

    def _resolve_message_id(self, short_or_full: str) -> str:
        return self._short_ids.resolve_message(short_or_full)

    def _resolve_request_id(self, short_or_full: str) -> str:
        return self._short_ids.resolve_request(short_or_full)

    # ── Reply bookkeeping ────────────────────────────────────────

    def _mark_reply(self, chat_id: str) -> None:
        """Record last reply time for ``chat_id`` (and ``chat_id:user_id``)."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        user_id = self._current_user.get(chat_id, "")
        if user_id:
            self._last_reply_times[f"{chat_id}:{user_id}"] = now
        self._last_reply_times[chat_id] = now

    # ── Notification out ─────────────────────────────────────────

    async def _write_notification(self, content: str, meta: dict) -> None:
        """Push a raw JSON-RPC notification to Claude Code."""
        if not self._write_stream:
            logger.warning("Write stream not ready, dropping notification")
            return
        from mcp.shared.session import SessionMessage
        from mcp.types import JSONRPCMessage, JSONRPCNotification

        notification = JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params={"content": content, "meta": meta},
        )
        await self._write_stream.send(SessionMessage(JSONRPCMessage(notification)))

    async def _send_channel_notification(self, content: str, meta: dict) -> None:
        """Queue a notification through the debounce pipeline."""
        if self._pipeline is None:
            logger.warning("Notification pipeline not ready, dropping notification")
            return
        await self._pipeline.send(content, meta)

    # ── Media dedup (md5 cache) ──────────────────────────────────

    def _check_media_hash(
        self, path, media_type: str, sender: str = ""
    ) -> str | None:
        """Compute md5; return a "duplicate of …" note if seen recently."""
        try:
            md5 = self._hashlib.md5(path.read_bytes()).hexdigest()
        except Exception:
            return None
        now = time.time()
        stale = [
            h for h, (_, _, t) in self._media_hashes.items() if now - t > 3600
        ]
        for h in stale:
            del self._media_hashes[h]
        if md5 in self._media_hashes:
            orig_path, orig_sender, _ = self._media_hashes[md5]
            return f" (duplicate of {orig_sender}'s earlier {media_type}: {orig_path})"
        self._media_hashes[md5] = (str(path), sender, now)
        return None

    # ── Media download (inbound) ─────────────────────────────────

    async def _download_feishu_media(
        self,
        content_json: str,
        message_type: str,
        message_id: str = "",
        sender: str = "",
    ) -> str:
        """Download Feishu media and return a description with local path.

        Preserves the legacy behavior in
        ``feishu_channel/server.py::_download_media``: image/audio/file/media,
        md5 dedup, short-video (≤10s) auto-transcription via ElevenLabs.
        """
        http = self.feishu.http
        token_provider = self.feishu.token
        temp_dir = self.settings.temp_dir

        for attempt in range(2):
            try:
                data = json.loads(content_json)
                token = await token_provider.get()
                msg_id = message_id or data.get("message_id", "")

                if message_type == "image":
                    path = await download_image(
                        http, token, msg_id, data["image_key"], temp_dir
                    )
                    dup = self._check_media_hash(path, "image", sender)
                    return f"[Image downloaded to {path}]{dup or ''}"

                elif message_type == "audio":
                    from .channels.feishu.media import download_audio
                    path = await download_audio(
                        http, token, msg_id, data["file_key"], temp_dir
                    )
                    transcript = ""
                    if self.settings.elevenlabs_api_key:
                        try:
                            transcript = await transcribe_audio(
                                http, self.settings.elevenlabs_api_key, path
                            )
                        except Exception as e:
                            logger.warning("Audio transcription failed: %s", e)
                    if transcript:
                        return f"[Voice message transcription: {transcript}]"
                    return f"[Audio file downloaded to {path}]"

                elif message_type == "media":
                    from pathlib import Path as _Path
                    raw_name = data.get("file_name", "video.mp4")
                    stem = _Path(raw_name).stem
                    suffix = _Path(raw_name).suffix or ".mp4"
                    file_name = f"{stem}_{int(time.time() * 1000)}{suffix}"
                    path = await download_file(
                        http, token, msg_id, data["file_key"], file_name, temp_dir
                    )
                    dup = self._check_media_hash(path, "video", sender)
                    transcript_note = ""
                    if self.settings.elevenlabs_api_key:
                        try:
                            import subprocess as _sp
                            probe = _sp.run(
                                [
                                    "ffprobe", "-v", "quiet",
                                    "-show_entries", "format=duration",
                                    "-of", "default=noprint_wrappers=1:nokey=1",
                                    str(path),
                                ],
                                capture_output=True, text=True, timeout=10,
                            )
                            duration = (
                                float(probe.stdout.strip())
                                if probe.stdout.strip() else 999
                            )
                            if duration <= 10:
                                audio_path = path.parent / f"{path.stem}_audio.opus"
                                _sp.run(
                                    ["ffmpeg", "-y", "-i", str(path),
                                     "-vn", "-acodec", "libopus", str(audio_path)],
                                    capture_output=True, timeout=15,
                                )
                                if audio_path.exists() and audio_path.stat().st_size > 0:
                                    transcript = await transcribe_audio(
                                        http,
                                        self.settings.elevenlabs_api_key,
                                        audio_path,
                                    )
                                    if transcript:
                                        transcript_note = f" [Audio: {transcript}]"
                                    audio_path.unlink(missing_ok=True)
                        except Exception as e:
                            logger.debug("Video audio transcription skipped: %s", e)
                    return f"[Video downloaded to {path}]{dup or ''}{transcript_note}"

                elif message_type == "file":
                    path = await download_file(
                        http, token, msg_id, data["file_key"],
                        data.get("file_name", ""), temp_dir,
                    )
                    dup = self._check_media_hash(path, "file", sender)
                    return f"[File downloaded to {path}]{dup or ''}"
            except Exception as e:
                if attempt == 0:
                    logger.info(
                        "Media download failed (attempt 1), refreshing token: %s", e
                    )
                    token_provider.invalidate()
                    continue
                logger.error("Media download failed: %s", e)
                return f"[Media download failed: {e}]"
        return content_json

    async def _process_post_content(self, content_json: str, message_id: str) -> str:
        """Parse inbound post (rich text), download embedded media."""
        http = self.feishu.http
        token_provider = self.feishu.token
        temp_dir = self.settings.temp_dir

        try:
            data = json.loads(content_json)
            post = data.get("content", [])
            title = data.get("title", "")
            token = await token_provider.get()

            lines: list[str] = []
            if title:
                lines.append(f"[Title: {title}]")
            media_idx = 0
            for line_elements in post:
                line_parts: list[str] = []
                for elem in line_elements:
                    tag = elem.get("tag", "")
                    if tag == "text":
                        line_parts.append(elem.get("text", ""))
                    elif tag == "a":
                        text = elem.get("text", "")
                        href = elem.get("href", "")
                        line_parts.append(f"{text}({href})")
                    elif tag == "at":
                        line_parts.append(
                            f"@{elem.get('user_name', elem.get('user_id', ''))}"
                        )
                    elif tag == "img":
                        image_key = elem.get("image_key", "")
                        if image_key and message_id:
                            try:
                                path = await download_image(
                                    http, token, message_id, image_key, temp_dir
                                )
                                line_parts.append(f"[Image downloaded to {path}]")
                            except Exception as e:
                                line_parts.append(
                                    f"[Image: {image_key} (download failed: {e})]"
                                )
                        else:
                            line_parts.append(f"[Image: {image_key}]")
                    elif tag == "media":
                        file_key = elem.get("file_key", "")
                        file_name = elem.get("file_name", f"media_{media_idx}.mp4")
                        if file_key and message_id:
                            try:
                                path = await download_file(
                                    http, token, message_id, file_key,
                                    file_name, temp_dir,
                                )
                                line_parts.append(f"[Video downloaded to {path}]")
                            except Exception as e:
                                line_parts.append(
                                    f"[Video: {file_key} (download failed: {e})]"
                                )
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

    # ── Parent-message lookup (for reply-to context) ─────────────

    async def _fetch_parent_content(self, parent_id: str) -> str | None:
        """Return text of the parent message for Feishu reply threads."""
        http = self.feishu.http
        token_provider = self.feishu.token
        try:
            token = await token_provider.get()
            resp = await http.get(
                f"https://open.feishu.cn/open-apis/im/v1/messages/{parent_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            data = resp.json()
            if data.get("code") != 0:
                return None
            items = data.get("data", {}).get("items", [])
            if not items:
                return None
            item = items[0]
            parent_body = item.get("body", {}).get("content", "")
            parent_type = item.get("msg_type", "text")
            try:
                parent_json = json.loads(parent_body)
                if parent_type == "text":
                    return parent_json.get("text", "")
                elif parent_type == "interactive":
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
                    return " ".join(texts) if texts else parent_body
                elif parent_type == "post":
                    texts = []
                    title = parent_json.get("title", "")
                    if title:
                        texts.append(title)
                    for row in parent_json.get("content", []):
                        if isinstance(row, list):
                            for el in row:
                                if isinstance(el, dict) and el.get("text"):
                                    texts.append(el["text"])
                    return " ".join(texts) if texts else parent_body
                else:
                    return parent_body
            except (json.JSONDecodeError, TypeError):
                return parent_body
        except Exception as e:
            logger.debug("Failed to fetch parent message %s: %s", parent_id, e)
            return None

    # ── Ingress handler (single entry point for all channels) ───

    async def _ingress(self, content: str, meta: dict[str, Any]) -> None:
        """Called by each channel's listener.

        Branches on ``meta['type']`` for card actions vs plain inbound messages,
        and on the channel suffix for WeChat vs Feishu enrichment.
        """
        # Card actions — adopt / create card, then forward as-is.
        if meta.get("type") == "card_action":
            chat_id = meta.get("chat_id", "")
            request_id = meta.get("request_id", "")
            open_message_id = meta.get("open_message_id")
            if open_message_id:
                await self.feishu.card_manager.adopt_card(
                    request_id, chat_id, open_message_id
                )
            else:
                await self.feishu.card_manager.create_card(request_id, chat_id, "")
            await self._send_channel_notification(content, meta)
            return

        chat_id = meta.get("chat_id", "")
        if chat_id.endswith("@im.wechat"):
            await self._ingress_wechat(content, meta)
        else:
            await self._ingress_feishu(content, meta)

    async def _ingress_feishu(self, content: str, meta: dict[str, Any]) -> None:
        """Full Feishu ingress pipeline — ported from ``_on_feishu_message``."""
        chat_id = meta["chat_id"]
        user_id = meta.get("user_id", "")
        chat_type = meta.get("chat_type", "")
        message_id = meta.get("message_id", "")
        request_id = meta.get("request_id", "") or str(uuid.uuid4())

        # Auto-add to heartbeat watchlist with a meaningful label
        user_alias = tools_profile.get_user_alias(user_id) if user_id else ""
        auto_label = ""
        if user_alias:
            if chat_type == "p2p":
                auto_label = f"{user_alias}p2p"
            elif chat_type == "group":
                auto_label = f"{user_alias}群"
            else:
                auto_label = user_alias
        tools_heartbeat.mark_activity(chat_id, label=auto_label)

        # Fetch parent message content if this is a quoted reply
        parent_id = meta.get("parent_id", "")
        if parent_id:
            parent_text = await self._fetch_parent_content(parent_id)
            if parent_text is not None:
                meta["reply_to_content"] = parent_text

        # Media / post enrichment
        message_type = meta.get("message_type", "")
        if message_type in ("image", "audio", "file", "media"):
            sender_alias = tools_profile.get_user_alias(user_id) or user_id
            content = await self._download_feishu_media(
                content, message_type, message_id, sender=sender_alias
            )
        elif message_type == "post":
            content = await self._process_post_content(content, message_id)

        # Defer card creation — only made when Claude calls reply_card
        self.feishu.card_manager.register_pending(request_id, chat_id, message_id)

        # Track current user for reply attribution
        if user_id:
            self._current_user[chat_id] = user_id

        # Queued-message detection: surface the last reply time
        last_reply_user = (
            self._last_reply_times.get(f"{chat_id}:{user_id}", "")
            if user_id else ""
        )
        last_reply_chat = self._last_reply_times.get(chat_id, "")
        last_reply = last_reply_user or last_reply_chat
        if last_reply:
            meta["last_reply_at"] = last_reply

        # Profile injection — throttled (1st msg, every 10th, or after 5 min)
        if user_id:
            profile = tools_profile.load_profile(chat_id, user_id)
            if profile:
                tools_profile.register_user_alias(user_id, profile)
            key = f"{chat_id}:{user_id}"
            count, last_time = self._profile_inject_state.get(key, (0, 0))
            now = time.time()
            if not profile:
                meta["user_profile"] = "NEW — create profile"
            elif count == 0 or count >= 10 or (now - last_time) > 300:
                meta["user_profile"] = profile
                self._profile_inject_state[key] = (1, now)
            else:
                user_name = tools_profile.get_user_alias(user_id)
                meta["user_profile"] = f"(see earlier profile for {user_name})"
                self._profile_inject_state[key] = (count + 1, last_time)

            user_name = tools_profile.get_user_alias(user_id)
            if user_name != user_id[:12]:
                meta["user_id"] = user_name

        # Replace chat_id with alias
        alias = tools_profile.get_alias(chat_id)
        if alias != chat_id[:12]:
            meta["chat_id"] = alias

        # Register short ids (#N, rN)
        short_msg, short_req = self._register_short_ids(message_id, request_id)
        meta["message_id"] = short_msg
        meta["request_id"] = short_req

        await self._send_channel_notification(content, meta)

    async def _ingress_wechat(self, content: str, meta: dict[str, Any]) -> None:
        """WeChat ingress — media already downloaded by the channel listener."""
        chat_id = meta.get("chat_id", "")
        user_id = meta.get("user_id", "")
        tools_heartbeat.mark_activity(chat_id, label=f"wechat:{user_id[:12]}")

        # Save to local history
        tools_messaging.save_wechat_message(content, meta, sender="user")

        request_id = meta.get("request_id", "") or str(uuid.uuid4())
        message_id = meta.get("message_id", "")

        short_msg, short_req = self._register_short_ids(message_id, request_id)
        meta["message_id"] = short_msg
        meta["request_id"] = short_req

        await self._send_channel_notification(content, meta)

    # ── MCP tool dispatcher ──────────────────────────────────────

    def _register_tools(self) -> None:
        orchestrator = self

        @self.server.list_tools()
        async def handle_list_tools() -> list[types.Tool]:
            return TOOLS

        @self.server.call_tool()
        async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
            # Resolve aliases / short ids — identical to legacy behavior.
            if "chat_id" in arguments and arguments["chat_id"]:
                arguments["chat_id"] = tools_profile.resolve_alias(arguments["chat_id"])
            if "user_id" in arguments and arguments["user_id"]:
                arguments["user_id"] = tools_profile.resolve_user_alias(arguments["user_id"])
            if "message_id" in arguments and arguments["message_id"]:
                arguments["message_id"] = orchestrator._resolve_message_id(arguments["message_id"])
            if "request_id" in arguments and arguments["request_id"]:
                arguments["request_id"] = orchestrator._resolve_request_id(arguments["request_id"])
            if "reply_to" in arguments and arguments.get("reply_to"):
                arguments["reply_to"] = orchestrator._resolve_message_id(arguments["reply_to"])

            result = await orchestrator._dispatch_tool(name, arguments)
            return [types.TextContent(type="text", text=json.dumps(result))]

    async def _dispatch_tool(self, name: str, arguments: dict) -> dict:
        """Branch on tool name. Assumes aliases already resolved."""
        # ── Messaging (dispatched through ChannelRegistry) ───
        if name == "reply":
            cid = arguments["chat_id"]
            channel = self.registry.get(cid)
            result = await tools_messaging.reply(
                channel, cid, arguments["text"], reply_to=arguments.get("reply_to")
            )
            if result.get("status") == "ok":
                self._mark_reply(cid)
            else:
                # Auto-remove from heartbeat watchlist when bot was kicked
                # (Feishu code 230002). Error message embeds the full JSON.
                msg = result.get("message", "")
                if "230002" in msg:
                    tools_heartbeat.manage_heartbeat("remove", cid)
            return result

        if name == "reply_image":
            cid = arguments["chat_id"]
            channel = self.registry.get(cid)
            return await tools_messaging.reply_image(channel, cid, arguments["image_path"])

        if name == "reply_file":
            cid = arguments["chat_id"]
            channel = self.registry.get(cid)
            return await tools_messaging.reply_file(channel, cid, arguments["file_path"])

        if name == "reply_video":
            cid = arguments["chat_id"]
            channel = self.registry.get(cid)
            return await tools_messaging.reply_video(channel, cid, arguments["video_path"])

        if name == "reply_post":
            cid = arguments["chat_id"]
            channel = self.registry.get(cid)
            return await tools_messaging.reply_post(
                channel, cid, arguments.get("title", ""), arguments["content"]
            )

        if name == "reply_audio":
            cid = arguments["chat_id"]
            channel = self.registry.get(cid)
            return await tools_messaging.reply_audio(channel, cid, arguments["text"])

        if name == "send_reaction":
            # Reactions go to Feishu — no chat_id to route by; always Feishu.
            return await tools_messaging.send_reaction(
                self.feishu, arguments["message_id"], arguments["emoji"]
            )

        if name == "read_messages":
            cid = arguments["chat_id"]
            # WeChat path doesn't need channel routing — it reads local jsonl.
            if cid.endswith("@im.wechat"):
                return tools_messaging.read_wechat_history(cid, arguments.get("count", 10))
            channel = self.registry.get(cid)
            return await tools_messaging.read_messages(channel, cid, arguments.get("count", 10))

        # ── Feishu cards (not channel-routed) ───────────────
        if name == "reply_card":
            rid = arguments["request_id"]
            result = await tools_cards.reply_card(
                self.feishu.card_manager,
                request_id=rid,
                text=arguments["text"],
                status=arguments.get("status", ""),
                done=arguments.get("done", False),
                emoji=arguments.get("emoji", "⏳"),
                template=arguments.get("template", "indigo"),
            )
            if result.get("status") == "ok":
                cid = tools_cards.resolve_card_chat_id(self.feishu.card_manager, rid)
                if cid:
                    self._mark_reply(cid)
            return result

        # ── Feishu docs / bitables / tasks ──────────────────
        if name == "create_doc":
            return await tools_docs.create_doc(
                self.feishu,
                arguments["title"],
                arguments.get("content", []),
                arguments.get("chat_id", ""),
            )
        if name == "create_bitable":
            return await tools_docs.create_bitable(
                self.feishu,
                arguments["title"],
                arguments.get("fields", []),
                arguments.get("records", []),
                arguments.get("views", []),
                arguments.get("chat_id", ""),
            )
        if name == "bitable_records":
            return await tools_docs.bitable_records(
                self.feishu,
                arguments["action"],
                arguments["app_token"],
                arguments["table_id"],
                arguments.get("records", []),
                arguments.get("filter", ""),
                arguments.get("page_size", 20),
            )
        if name == "manage_task":
            return await tools_docs.manage_task(
                self.feishu,
                arguments["action"],
                arguments.get("summary", ""),
                arguments.get("description", ""),
                arguments.get("due", ""),
                arguments.get("task_id", ""),
                arguments.get("page_size", 20),
            )
        if name == "search_docs":
            return await tools_docs.search_docs(self.feishu, arguments["query"])

        # ── Reminders (Boss-only create/delete) ─────────────
        if name == "create_reminder":
            # Caller user_id lives on ``_current_user[chat_id]`` when the tool
            # is invoked in response to an inbound message. Fall back to the
            # target chat's current user.
            chat_id = arguments["chat_id"]
            caller = self._current_user.get(chat_id, "")
            return tools_reminders.create_reminder(
                caller,
                arguments["reminder_id"],
                arguments["cron_expression"],
                arguments["chat_id"],
                arguments["message"],
                smart=arguments.get("smart", False),
                max_runs=arguments.get("max_runs", 0),
            )
        if name == "list_reminders":
            return tools_reminders.list_reminders()
        if name == "delete_reminder":
            # No chat_id in args — find any chat with Boss as current user.
            caller = next(
                (u for u in self._current_user.values()
                 if u == tools_reminders.BOSS_USER_ID),
                "",
            )
            return tools_reminders.delete_reminder(caller, arguments["reminder_id"])

        # ── Profile ─────────────────────────────────────────
        if name == "update_profile":
            return tools_profile.update_profile(
                arguments["chat_id"],
                arguments["user_id"],
                name=arguments["name"],
                title=arguments.get("title", ""),
                real_name=arguments.get("real_name", ""),
                location=arguments.get("location", ""),
                phone=arguments.get("phone", ""),
                notes=arguments.get("notes", ""),
            )
        if name == "get_user_info":
            return await tools_profile.get_user_info(
                self.feishu,
                arguments["user_id"],
                arguments.get("download_avatar", False),
            )

        # ── Heartbeat ───────────────────────────────────────
        if name == "manage_heartbeat":
            return tools_heartbeat.manage_heartbeat(
                arguments["action"],
                arguments.get("chat_id", ""),
                arguments.get("label", ""),
                arguments.get("interval", 0),
            )

        # ── Image search ────────────────────────────────────
        if name == "search_image":
            return await tools_media_search.search_image(
                self.feishu.http,
                self.settings.pexels_api_key,
                self.settings.tenor_api_key,
                arguments["query"],
                arguments.get("type", "photo"),
                arguments.get("count", 1),
            )

        # ── WeChat login ────────────────────────────────────
        if name == "wechat_login_qr":
            return await tools_wechat_login.wechat_login_qr(
                self.wechat,
                arguments.get("account_id", "new"),
                arguments.get("chat_id", ""),
            )

        return {"status": "error", "message": f"Unknown tool: {name}"}

    # ── Periodic tasks ───────────────────────────────────────────

    async def _periodic_cleanup(self) -> None:
        """Run cleanup tasks every 30 minutes."""
        while True:
            await asyncio.sleep(1800)
            try:
                cleanup_old_files(
                    self.settings.temp_dir, self.settings.temp_file_max_age_hours
                )
                await self.feishu.card_manager.cleanup_stale_cards()
            except Exception as e:
                logger.error("Cleanup error: %s", e)

    # ── Main ─────────────────────────────────────────────────────

    async def run(self) -> None:
        """Start channels + MCP stdio server."""
        loop = asyncio.get_running_loop()

        # Spin up every registered channel's listener, all going through _ingress.
        for channel in self.registry:
            await channel.start(loop, self._ingress)

        # Background tasks
        asyncio.create_task(self._periodic_cleanup())

        # Heartbeat loop
        tools_heartbeat.configure_inactivity(
            self.settings.heartbeat_inactivity_minutes
        )
        asyncio.create_task(
            tools_heartbeat.heartbeat_loop(
                interval_minutes=self.settings.heartbeat_interval_minutes,
                notify_fn=self._send_channel_notification,
            )
        )

        # Scheduled-task watcher
        asyncio.create_task(
            tools_reminders.watch_scheduled_tasks(
                send_notification=self._send_channel_notification,
                create_card=self.feishu.card_manager.create_card,
            )
        )

        # Connect MCP stdio
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            self._write_stream = write_stream
            self._pipeline = NotificationPipeline(self._write_notification)
            init_options = self.server.create_initialization_options(
                experimental_capabilities={"claude/channel": {}}
            )
            await self.server.run(read_stream, write_stream, init_options)


# ── Public factory + main entry ─────────────────────────────────


def build_server() -> XiaobaiServer:
    """Construct the server (no network I/O). Used by the smoke test."""
    return XiaobaiServer()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler("/tmp/xiaobai.log"),
        ],
    )
    server = build_server()
    asyncio.run(server.run())


if __name__ == "__main__":
    main()
