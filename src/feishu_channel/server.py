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
        description="Update the Feishu card with your current response text. Call this after EVERY message you send to the user.",
        inputSchema={
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "The request_id from the inbound message"},
                "status": {"type": "string", "description": "Short status shown in card header, e.g. 'Thinking...', 'Searching codebase...', 'Writing code...'"},
                "text": {"type": "string", "description": "Your current response text"},
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
                result = await cards.update_card(arguments["request_id"], arguments["status"], arguments["text"])
            elif name == "reply":
                result = await cards.finalize_card(arguments["request_id"], arguments["text"])
            elif name == "reply_file":
                result = await cards.upload_and_send_file(arguments["chat_id"], arguments["file_path"])
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
        """Called when a Feishu message arrives. Creates card and pushes notification."""
        mark_activity()
        chat_id = meta["chat_id"]
        message_id = meta.get("message_id", "")

        # Handle media downloads
        message_type = meta["message_type"]
        if message_type in ("image", "audio", "file"):
            content = await self._download_media(content, message_type)

        # Pre-create the card (shows "thinking..." immediately)
        await self.cards.create_card(request_id, chat_id, message_id)

        # Push notification to Claude Code via raw write stream
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

    async def _download_media(self, content_json: str, message_type: str) -> str:
        """Download media and return a description with the local file path."""
        try:
            data = json.loads(content_json)
            token = await self.cards._get_token()
            temp_dir = self.settings.temp_dir

            if message_type == "image":
                path = await download_image(
                    self.http, token, data["message_id"], data["image_key"], temp_dir
                )
                return f"[Image downloaded to {path}]"
            elif message_type == "audio":
                path = await download_audio(
                    self.http, token, data["message_id"], data["file_key"], temp_dir
                )
                return f"[Audio file downloaded to {path}]"
            elif message_type == "file":
                path = await download_file(
                    self.http, token, data["message_id"], data["file_key"],
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
