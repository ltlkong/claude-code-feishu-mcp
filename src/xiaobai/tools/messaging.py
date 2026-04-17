"""Messaging tools — reply / reply_image / reply_file / reply_video / reply_post /
reply_audio / send_reaction / read_messages.

All handlers dispatch through the ``Channel`` protocol (Session 1). The
send_* work is done inside each channel adapter, so this module is mostly
glue + per-tool post-processing (history persistence, heartbeat-remove on
``not a member`` errors, etc.).
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _is_wechat_id(chat_id: str) -> bool:
    return chat_id.endswith("@im.wechat")


# ── WeChat local jsonl history ───────────────────────────────────
# Preserved from ``feishu_channel/server.py::_save_wechat_message`` /
# ``_read_wechat_history``. Used for history persistence of both inbound
# and outbound WeChat messages.


_HISTORY_DIR = Path("workspace/state/wechat_history")


def save_wechat_message(content: str, meta: dict, sender: str = "user") -> None:
    """Append a WeChat message to ``workspace/state/wechat_history/<chat>.jsonl``."""
    try:
        chat_id = meta.get("chat_id", meta.get("user_id", "unknown"))
        safe_id = chat_id.replace("@", "_").replace("/", "_")
        _HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        path = _HISTORY_DIR / f"{safe_id}.jsonl"
        entry = {
            "ts": meta.get(
                "message_time",
                time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            ),
            "sender": sender,
            "user_id": meta.get("user_id", ""),
            "type": meta.get("message_type", "text"),
            "content": content[:2000],
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error("Failed to save WeChat message: %s", e)


def read_wechat_history(chat_id: str, count: int = 10, keyword: str = "") -> dict:
    """Read local WeChat jsonl history in reverse-chronological order."""
    safe_id = chat_id.replace("@", "_").replace("/", "_")
    path = _HISTORY_DIR / f"{safe_id}.jsonl"
    if not path.is_file():
        return {"status": "ok", "count": 0, "messages": [], "source": "local"}
    try:
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        messages: list[dict] = []
        for line in reversed(lines):
            if not line.strip():
                continue
            entry = json.loads(line)
            if keyword and keyword.lower() not in entry.get("content", "").lower():
                continue
            messages.append({
                "sender_id": entry.get("user_id", ""),
                "sender_type": "app" if entry.get("sender") == "bot" else "user",
                "msg_type": entry.get("type", "text"),
                "text": entry.get("content", ""),
                "create_time": entry.get("ts", ""),
            })
            if len(messages) >= count:
                break
        return {
            "status": "ok",
            "count": len(messages),
            "messages": messages,
            "source": "local",
        }
    except Exception as e:
        return {"status": "error", "message": f"Read WeChat history error: {e}"}


# ── Tool handlers ────────────────────────────────────────────────


async def reply(channel, chat_id: str, text: str, reply_to: str | None = None) -> dict:
    """Send a text message. Dispatches through ``channel.send_text``.

    For WeChat channels we persist the outbound text to local jsonl history
    so ``read_messages`` can find it later. For Feishu, if the API returns
    230002 (not a member) we flag that in the response so the orchestrator
    can auto-remove the chat from the heartbeat watchlist.
    """
    result = await channel.send_text(chat_id, text, reply_to=reply_to)
    # Preserve legacy: WeChat outbound goes to the local log
    if _is_wechat_id(chat_id) and result.get("status") == "ok":
        save_wechat_message(
            text,
            {
                "chat_id": chat_id,
                "user_id": chat_id,
                "message_type": "text",
                "message_time": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            },
            sender="bot",
        )
    return result


async def reply_image(channel, chat_id: str, image_path: str) -> dict:
    return await channel.send_image(chat_id, image_path)


async def reply_file(channel, chat_id: str, file_path: str) -> dict:
    return await channel.send_file(chat_id, file_path)


async def reply_video(channel, chat_id: str, video_path: str) -> dict:
    return await channel.send_video(chat_id, video_path)


async def reply_post(channel, chat_id: str, title: str, content: list) -> dict:
    return await channel.send_post(chat_id, title, content)


async def reply_audio(channel, chat_id: str, text: str) -> dict:
    return await channel.send_audio_tts(chat_id, text)


async def send_reaction(channel, message_id: str, emoji: str) -> dict:
    return await channel.send_reaction(message_id, emoji)


async def read_messages(channel, chat_id: str, count: int = 10) -> dict:
    """Feishu → live REST; WeChat → local jsonl."""
    if _is_wechat_id(chat_id):
        return read_wechat_history(chat_id, count)
    msgs = await channel.read_history(chat_id, count)
    return {"status": "ok", "count": len(msgs), "messages": msgs}
