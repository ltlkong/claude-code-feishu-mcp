"""Feishu WebSocket listener with dedup, sender gating, and card action monkey-patch.

Runs lark-oapi WebSocket in a background thread. Calls `on_message` and `on_card_action`
callbacks when events arrive (after dedup and gating checks).
"""

import asyncio
import json
import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Callable, Awaitable

import httpx
import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import (
    P2CardActionTrigger, P2CardActionTriggerResponse, CallBackToast,
)
from lark_oapi.ws.client import HEADER_TYPE, _get_by_key

from .config import Settings

logger = logging.getLogger(__name__)

# ── Monkey-patch: route card action frames through event dispatcher ──
# lark-oapi's _handle_data_frame silently drops MessageType.CARD frames.
# We rewrite the type header from "card" to "event" so they reach our handler.
# Tested with lark-oapi 1.5.x — if the library changes _handle_data_frame,
# this patch may break. Check after upgrading lark-oapi.

if not hasattr(lark.ws.Client, "_handle_data_frame"):
    logger.warning("lark-oapi API changed: _handle_data_frame not found, card actions may not work")

_original_handle = getattr(lark.ws.Client, "_handle_data_frame", None)


if _original_handle:
    async def _patched_handle_data_frame(self, frame):
        type_ = _get_by_key(frame.headers, HEADER_TYPE)
        if type_ == "card":
            for h in frame.headers:
                if h.key == HEADER_TYPE:
                    h.value = "event"
                    break
        return await _original_handle(self, frame)

    lark.ws.Client._handle_data_frame = _patched_handle_data_frame


# ── Dedup cache ──────────────────────────────────────────────────────

class _DedupCache:
    """OrderedDict-based dedup with TTL. Thread-safe."""

    def __init__(self, max_size: int = 1000, ttl_seconds: float = 60):
        self._cache: OrderedDict[str, float] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = threading.Lock()

    def seen(self, key: str) -> bool:
        """Return True if key was already seen (not expired). Adds key if new."""
        with self._lock:
            now = time.time()
            # Evict expired
            while self._cache:
                oldest_key, oldest_time = next(iter(self._cache.items()))
                if now - oldest_time > self._ttl:
                    self._cache.popitem(last=False)
                else:
                    break
            # Check
            if key in self._cache:
                return True
            # Add
            self._cache[key] = now
            if len(self._cache) > self._max_size:
                self._cache.popitem(last=False)
            return False


# ── Card action element parsing ──────────────────────────────────────

def _parse_card_action(data: P2CardActionTrigger) -> tuple[str, dict]:
    """Parse a card action into (text_description, meta_dict)."""
    event = data.event
    action = event.action
    tag = action.tag
    name = action.name or tag
    value = action.value

    # Convert different action types to readable text
    if tag == "button":
        text = f"[User clicked button ({name}): {value}]"
    elif tag.startswith("select"):
        text = f"[User selected ({name}): {value}]"
    elif tag == "input":
        text = f"[User input ({name}): {value}]"
    elif tag == "form":
        # Form submissions have multiple values
        form_data = ", ".join(f"{k}={v}" for k, v in (value or {}).items())
        text = f"[User submitted form ({name}): {form_data}]"
    elif tag in ("date_picker", "time_picker"):
        text = f"[User picked date/time ({name}): {value}]"
    elif tag in ("checker", "multi_select_static", "multi_select_person"):
        text = f"[User multi-selected ({name}): {value}]"
    else:
        text = f"[Card action ({tag}): {json.dumps(value)}]"

    request_id = str(uuid.uuid4())
    meta = {
        "type": "card_action",
        "chat_id": event.context.open_chat_id,
        "user_id": event.operator.open_id,
        "request_id": request_id,
        "action_tag": tag,
        "action_value": json.dumps(value) if not isinstance(value, str) else value,
        "open_message_id": event.context.open_message_id,
    }
    return text, meta


# ── Message content parsing ──────────────────────────────────────────

def parse_message_content(msg) -> tuple[str, str]:
    """Parse a Feishu message event into (message_type, content_text).

    For text messages, returns the text directly.
    For media, returns a placeholder — the actual download happens async.
    """
    msg_type = msg.message.message_type
    content_str = msg.message.content

    try:
        content = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        return msg_type, content_str or ""

    if msg_type == "text":
        return "text", content.get("text", "")
    elif msg_type == "image":
        image_key = content.get("image_key", "")
        return "image", json.dumps({"image_key": image_key, "message_id": msg.message.message_id})
    elif msg_type == "audio":
        file_key = content.get("file_key", "")
        return "audio", json.dumps({"file_key": file_key, "message_id": msg.message.message_id})
    elif msg_type == "file":
        file_key = content.get("file_key", "")
        file_name = content.get("file_name", "")
        return "file", json.dumps({"file_key": file_key, "file_name": file_name, "message_id": msg.message.message_id})
    else:
        return msg_type, content_str or ""



# ── Feishu Listener ──────────────────────────────────────────────────

OnMessageCallback = Callable[[str, str, dict], Awaitable[None]]
# (content, request_id, meta) -> None

OnCardActionCallback = Callable[[str, dict], Awaitable[None]]
# (content, meta) -> None


class FeishuListener:
    """Manages the Feishu WebSocket connection and dispatches events."""

    def __init__(
        self,
        settings: Settings,
        on_message: OnMessageCallback,
        on_card_action: OnCardActionCallback,
    ):
        self._settings = settings
        self._on_message = on_message
        self._on_card_action = on_card_action
        self._dedup = _DedupCache()
        self._ws_client: lark.ws.Client | None = None

    def _is_allowed(self, user_id: str) -> bool:
        if not self._settings.allowed_user_ids:
            return True
        return user_id in self._settings.allowed_user_ids

    def _handle_message(self, data: P2ImMessageReceiveV1) -> None:
        """Sync handler called by lark-oapi. Dispatches async work."""
        event = data.event
        msg = event

        # Dedup
        msg_id = msg.message.message_id
        if self._dedup.seen(msg_id):
            return

        # Sender gating
        sender_id = msg.sender.sender_id.open_id
        if not self._is_allowed(sender_id):
            return

        # Parse
        message_type, content = parse_message_content(msg)
        request_id = str(uuid.uuid4())

        from datetime import datetime, timezone
        message_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        meta = {
            "user_id": sender_id,
            "chat_id": msg.message.chat_id,
            "chat_type": msg.message.chat_type or "p2p",  # "p2p" or "group"
            "sender_name": getattr(msg.sender, "sender_id", None) and msg.sender.sender_id.open_id or "unknown",
            "message_type": message_type,
            "message_time": message_time,
            "request_id": request_id,
            "message_id": msg.message.message_id,
            "root_id": msg.message.root_id or "",  # thread root message id (for replies)
            "parent_id": msg.message.parent_id or "",  # direct parent message id (for replies)
        }

        # Track active chats and last message time for recovery
        if hasattr(self, "_active_chats"):
            self._active_chats[msg.message.chat_id] = time.time()
        if hasattr(self, "_last_ws_msg_time") and msg.message.create_time:
            try:
                self._last_ws_msg_time[msg.message.chat_id] = int(msg.message.create_time)
            except (ValueError, TypeError):
                pass

        # Fire async callback — we're in lark-oapi's thread, schedule on MCP's event loop
        if hasattr(self, "_loop") and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._on_message(content, request_id, meta), self._loop
            )

    def _handle_card_action(self, data: P2CardActionTrigger) -> P2CardActionTriggerResponse:
        """Handle card button clicks and form submissions."""
        event = data.event
        user_id = event.operator.open_id
        if not self._is_allowed(user_id):
            resp = P2CardActionTriggerResponse()
            return resp

        content, meta = _parse_card_action(data)

        if hasattr(self, "_loop") and self._loop:
            asyncio.run_coroutine_threadsafe(
                self._on_card_action(content, meta), self._loop
            )

        # Return minimal toast
        resp = P2CardActionTriggerResponse()
        toast = CallBackToast()
        toast.type = "info"
        toast.content = " "
        resp.toast = toast
        return resp

    def start(self, loop) -> None:
        """Start the WebSocket listener in a background thread.

        Args:
            loop: The asyncio event loop running the MCP server (for scheduling callbacks).
        """
        self._loop = loop
        self._active_chats: dict[str, float] = {}  # chat_id -> last_activity_time (for eviction)
        self._last_ws_msg_time: dict[str, int] = {}  # chat_id -> last message create_time (ms timestamp as int)
        self._http = httpx.AsyncClient(timeout=30)
        self._token: str | None = None
        self._token_time: float = 0

        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._handle_message)
            .register_p2_card_action_trigger(self._handle_card_action)
            .build()
        )
        self._ws_client = lark.ws.Client(
            self._settings.feishu_app_id,
            self._settings.feishu_app_secret,
            event_handler=handler,
            log_level=lark.LogLevel.INFO,
        )

        thread = threading.Thread(target=self._ws_client.start, daemon=True)
        thread.start()
        logger.info("Feishu WebSocket listener started")

        # Start message recovery loop
        asyncio.ensure_future(self._message_recovery_loop())

    async def _get_token(self) -> str:
        """Get cached tenant access token."""
        if self._token and (time.time() - self._token_time) < 5400:
            return self._token
        from .media import get_tenant_token
        self._token = await get_tenant_token(
            self._http, self._settings.feishu_app_id, self._settings.feishu_app_secret
        )
        self._token_time = time.time()
        return self._token

    async def _pull_recent_messages(self, chat_id: str, count: int = 5) -> list[dict]:
        """Pull recent messages from a chat via REST API."""
        try:
            token = await self._get_token()
            resp = await self._http.get(
                f"https://open.feishu.cn/open-apis/im/v1/messages",
                params={
                    "container_id_type": "chat",
                    "container_id": chat_id,
                    "sort_type": "ByCreateTimeDesc",
                    "page_size": count,
                },
                headers={"Authorization": f"Bearer {token}"},
            )
            data = resp.json()
            if data.get("code") == 0:
                return data.get("data", {}).get("items", [])
        except Exception as e:
            logger.debug("Pull messages failed for %s: %s", chat_id, e)
        return []

    async def _recover_missed_messages(self):
        """Check active chats for any messages missed during WebSocket gaps.
        Only processes messages NEWER than the last WebSocket-received message per chat."""
        if not self._active_chats:
            return

        # Evict chats inactive for more than 2 hours
        now = time.time()
        stale = [cid for cid, t in self._active_chats.items() if now - t > 7200]
        for cid in stale:
            del self._active_chats[cid]
            self._last_ws_msg_time.pop(cid, None)

        recovered = 0
        for chat_id in list(self._active_chats):
            last_ws_time = self._last_ws_msg_time.get(chat_id)
            if not last_ws_time:
                continue  # No baseline yet — skip until we've received at least one WS message

            messages = await self._pull_recent_messages(chat_id, count=5)
            for msg in messages:
                msg_id = msg.get("message_id", "")
                if not msg_id:
                    continue

                # Only process messages NEWER than last WebSocket message (int comparison)
                try:
                    msg_ts = int(msg.get("create_time", 0))
                except (ValueError, TypeError):
                    continue
                if msg_ts <= last_ws_time:
                    continue

                # Check dedup (secondary filter, thread-safe)
                if self._dedup.seen(msg_id):
                    continue

                # Skip bot's own messages
                sender = msg.get("sender", {})
                sender_id = sender.get("id", "")
                if sender.get("sender_type") == "app":
                    continue
                if not self._is_allowed(sender_id):
                    continue

                msg_type = msg.get("msg_type", "text")
                body = msg.get("body", {})
                content_str = body.get("content", "")

                try:
                    content_json = json.loads(content_str)
                    if msg_type == "text":
                        content = content_json.get("text", "")
                    else:
                        content = content_str
                except (json.JSONDecodeError, TypeError):
                    content = content_str

                from datetime import datetime, timezone
                request_id = str(uuid.uuid4())
                meta = {
                    "user_id": sender_id,
                    "chat_id": chat_id,
                    "chat_type": msg.get("chat_type", "p2p"),
                    "sender_name": sender_id,
                    "message_type": msg_type,
                    "message_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "request_id": request_id,
                    "message_id": msg_id,
                }

                logger.info("Recovered missed message: %s in %s (ts=%d > last_ws=%d)",
                           msg_id, chat_id, msg_ts, last_ws_time)
                # Already in asyncio loop — use ensure_future, not run_coroutine_threadsafe
                asyncio.ensure_future(self._on_message(content, request_id, meta))
                recovered += 1

        if recovered:
            logger.info("Message recovery: recovered %d missed messages", recovered)

    async def _message_recovery_loop(self):
        """Periodically check for missed messages (every 60 seconds)."""
        await asyncio.sleep(30)  # Initial delay
        while True:
            try:
                await self._recover_missed_messages()
            except Exception as e:
                logger.error("Message recovery loop error: %s", e)
            await asyncio.sleep(60)
