"""WeChat message listener via iLink long-polling.

Ported verbatim from ``wechat_channel/listener.py``. Backoff + crash-recovery
semantics preserved. ``_wechat_media_item`` is still stashed in meta for the
channel adapter to pop and pass to ``download_media``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

import httpx

from .ilink import (
    ILinkClient,
    ILinkProtocolError,
    MSG_FILE,
    MSG_IMAGE,
    MSG_TEXT,
    MSG_VIDEO,
    MSG_VOICE,
)

logger = logging.getLogger(__name__)

# Backoff settings
MIN_BACKOFF = 1.0
MAX_BACKOFF = 60.0
IDLE_POLL_SLEEP_SECONDS = 0.3

MSG_TYPE_NAMES = {
    MSG_TEXT: "text",
    MSG_IMAGE: "image",
    MSG_VOICE: "voice",
    MSG_FILE: "file",
    MSG_VIDEO: "video",
}


class WeChatListener:
    """Long-poll listener for WeChat messages via iLink API."""

    def __init__(
        self,
        client: ILinkClient,
        on_message: Callable[[str, dict[str, Any]], Awaitable[None]],
    ) -> None:
        self.client = client
        self.on_message = on_message
        self._backoff = MIN_BACKOFF
        self._running = False
        self._poll_count = 0

    async def start(self) -> None:
        """Start the long-poll loop with auto-recovery. Runs forever."""
        self._running = True
        logger.info("WeChat listener starting")

        while self._running:
            try:
                await self._poll_loop()
            except asyncio.CancelledError:
                logger.info("WeChat listener cancelled")
                raise
            except BaseException as e:
                logger.error("WeChat listener crashed: %s", e, exc_info=True)
                if not self._running:
                    break
                await asyncio.sleep(5.0)
                logger.info("WeChat listener restarting after crash")

        logger.info("WeChat listener stopped (polls=%d)", self._poll_count)

    async def _poll_loop(self) -> None:
        """Inner poll loop — separated so start() can catch and restart."""
        while self._running:
            try:
                messages = await self.client.get_updates()
                self._poll_count += 1
                self._backoff = MIN_BACKOFF

                if messages:
                    logger.info("WeChat got %d messages", len(messages))

                for msg in messages:
                    try:
                        await self._handle_message(msg)
                    except Exception as e:
                        logger.error("Error handling WeChat message: %s", e, exc_info=True)

                if self._poll_count % 100 == 0:
                    logger.info("WeChat listener alive (polls=%d)", self._poll_count)
                if not messages:
                    # Guard against hot-looping when upstream returns immediately.
                    await asyncio.sleep(IDLE_POLL_SLEEP_SECONDS)

            except ILinkProtocolError as e:
                if e.errcode in (-14,):
                    logger.error(
                        "WeChat session expired (errcode=%d), need re-login",
                        e.errcode,
                    )
                    self._running = False
                    break
                logger.warning("iLink getupdates protocol error: %s", e)
                await self._do_backoff()

            except httpx.HTTPStatusError as e:
                if e.response.status_code in (401, 403):
                    logger.error(
                        "WeChat auth expired (HTTP %d), need re-login",
                        e.response.status_code,
                    )
                    self._running = False
                    break
                elif e.response.status_code == 429:
                    logger.warning("Rate limited, backing off")
                    await self._do_backoff()
                else:
                    logger.error("HTTP error in poll: %s", e)
                    await self._do_backoff()

            except (httpx.TimeoutException, httpx.ConnectError) as e:
                logger.debug("Poll connection issue: %s", type(e).__name__)
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                raise

            except Exception as e:
                logger.error("Unexpected poll error: %s", e, exc_info=True)
                await self._do_backoff()

    def stop(self) -> None:
        logger.info("WeChat listener stop requested")
        self._running = False

    async def _do_backoff(self) -> None:
        await asyncio.sleep(self._backoff)
        self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    async def _handle_message(self, msg: dict) -> None:
        """Parse a raw iLink message and dispatch to callback."""
        from_user = msg.get("from_user_id", "")
        msg_state = msg.get("message_state", 0)
        msg_type_val = msg.get("message_type", 0)
        item_list = msg.get("item_list", [])

        logger.info(
            "WeChat msg: from=%s type=%s state=%s items=%d",
            from_user[:20] if from_user else "?", msg_type_val, msg_state, len(item_list),
        )

        # Skip generating messages, only process FINISH (2) or NEW (0)
        if msg_state not in (0, 2):
            logger.info("WeChat msg: skipped (state=%s)", msg_state)
            return

        # Skip bot's own messages (message_type 2 = BOT)
        if msg_type_val == 2:
            logger.info("WeChat msg: skipped (bot message)")
            return

        for item in item_list:
            item_type = item.get("type", 0)
            type_name = MSG_TYPE_NAMES.get(item_type, f"unknown_{item_type}")

            if item_type == MSG_TEXT:
                text_item = item.get("text_item", {})
                content = text_item.get("text", "")
            elif item_type == MSG_IMAGE:
                content = "[Image from WeChat]"
            elif item_type == MSG_VOICE:
                voice_item = item.get("voice_item", {})
                transcription = voice_item.get("text", "")
                content = (
                    f"[Voice message: {transcription}]" if transcription
                    else "[Voice message]"
                )
            elif item_type == MSG_FILE:
                file_item = item.get("file_item", {})
                filename = file_item.get("file_name", "unknown")
                content = f"[File: {filename}]"
            elif item_type == MSG_VIDEO:
                content = "[Video from WeChat]"
            else:
                content = f"[Unknown message type {item_type}]"

            meta = {
                "source": "wechat",
                "user_id": from_user,
                "chat_id": from_user,  # WeChat p2p uses user_id as chat_id
                "chat_type": "p2p",
                "message_type": type_name,
                "message_time": _format_time(msg.get("create_time_ms", 0)),
                "message_id": str(msg.get("message_id", "")),
            }

            # Include media info for download (image/file/video/voice)
            if item_type in (MSG_IMAGE, MSG_FILE, MSG_VIDEO, MSG_VOICE):
                meta["_wechat_media_item"] = item

            logger.info("WeChat dispatching: %s", content[:80])
            await self.on_message(content, meta)


def _format_time(ms_timestamp: int) -> str:
    """Convert ms timestamp to readable UTC time string."""
    if not ms_timestamp:
        return ""
    ts = ms_timestamp / 1000
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))
