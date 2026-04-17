"""Feishu adapter — exposes a :class:`Channel` interface over the Feishu API.

Every send_* method here was ported (copy-modify) from the corresponding
``_handle_*`` method on ``feishu_channel.server.FeishuChannel``.  Behavior is
preserved: retry-on-token-error, markdown → post format auto-detection,
ffmpeg thumbnail extraction, ElevenLabs TTS, etc.

The listener and card manager share a single :class:`TokenProvider` via
``api.fetch_tenant_token``, eliminating the duplicate token caches that
used to live in ``card.py`` and ``feishu.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess as _sp
import uuid
from pathlib import Path
from typing import Any

import httpx

from ...core.auth import TokenProvider
from ...core.channel import Capabilities, OnMessageCallback
from .api import fetch_tenant_token, is_token_error
from .cards import CardManager
from .listener import FeishuListener
from .media import (
    download_audio,
    download_file,
    download_image,
    text_to_speech,
    transcribe_audio,
)

logger = logging.getLogger(__name__)


def _is_wechat_id(chat_id: str) -> bool:
    """Detect if a chat_id belongs to WeChat (ends with @im.wechat)."""
    return chat_id.endswith("@im.wechat")


def _unwrap_post_body(data: dict[str, Any]) -> dict[str, Any]:
    """Return the localized Feishu post body from a raw content payload."""
    if "content" in data or "title" in data:
        return data
    for locale in ("zh_cn", "en_us", "ja_jp"):
        body = data.get(locale)
        if isinstance(body, dict):
            return body
    return data


class FeishuChannel:
    """Feishu Channel adapter.

    Instances own a :class:`CardManager`, a :class:`FeishuListener`, a
    shared ``httpx.AsyncClient`` and a :class:`TokenProvider`. The
    ``card_manager`` attribute is intentionally exposed publicly so the
    Session 2 tool layer can drive ``reply_card`` through it.
    """

    id = "feishu"
    capabilities = Capabilities(
        has_cards=True,
        has_reactions=True,
        has_audio=True,
        has_video=True,
        has_post=True,
        has_reply_to=True,
        has_read_history_api=True,
    )

    # ── Markdown / emoji parsing regexes (copied from legacy server.py) ──

    _MD_PATTERN = re.compile(
        r'\*\*.*?\*\*|\*.*?\*|~~.*?~~|`[^`]+`|^- |^\d+\. |^> |^#{1,6} |!\[.*?\]\(.*?\)|\[.*?\]\(.*?\)',
        re.MULTILINE,
    )
    _EMOJI_MAP = {
        "送心": "HEART", "赞": "THUMBSUP", "大笑": "LAUGH", "比心": "FINGERHEART",
        "酷": "COOL", "OK": "OK", "撇嘴": "POUT", "抠鼻": "NOSEPICK",
        "呲牙": "GRIN", "机智": "SMART", "偷笑": "LMAO", "发怒": "ANGRY",
        "害羞": "SHY", "流泪": "CRY", "惊讶": "SURPRISE", "亲亲": "KISS",
        "鼓掌": "CLAP", "强壮": "MUSCLE", "庆祝": "PARTY", "干杯": "BEER",
        "咖啡": "COFFEE", "玫瑰": "ROSE", "太阳": "SUN", "月亮": "MOON",
        "火": "Fire", "爱心": "HEART",
    }

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        allowed_user_ids: list[str] | None = None,
        temp_dir: Path = Path("/tmp/feishu-channel"),
        elevenlabs_api_key: str = "",
        elevenlabs_voice_id: str = "",
        stale_card_timeout_minutes: int = 30,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._app_id = app_id
        self._app_secret = app_secret
        self._allowed_user_ids = allowed_user_ids or []
        self._temp_dir = temp_dir
        self._elevenlabs_api_key = elevenlabs_api_key
        self._elevenlabs_voice_id = elevenlabs_voice_id

        # Shared httpx client used by cards + listener + send_* methods.
        # timeout=30 matches the legacy recovery-loop client (feishu.py:356);
        # the card manager's legacy timeout was 10 but the longer window is
        # strictly more tolerant, so no regressions either way.
        self._http = http or httpx.AsyncClient(timeout=30)

        # Shared token provider — single-flight refresh
        self._token = TokenProvider[str](
            name="feishu",
            fetch=lambda http: fetch_tenant_token(http, app_id, app_secret),
            http=self._http,
        )

        # Card manager (drives reply_card)
        self.card_manager = CardManager(
            token_provider=self._token,
            http=self._http,
            stale_timeout_minutes=stale_card_timeout_minutes,
        )

        # Listener (WebSocket + recovery loop). Callbacks are wired in start().
        self._listener: FeishuListener | None = None

    # ── Ownership ────────────────────────────────────────────────

    def owns(self, chat_id: str) -> bool:
        """Feishu owns any ``oc_...`` chat_id that is NOT a WeChat chat_id.

        In practice both p2p and group Feishu chats use ``oc_`` as their
        chat_id prefix (verified against workspace/state/heartbeat_watchlist.json).
        The legacy server also accepted ``ou_`` as a p2p hint; we keep that
        for defensive compatibility.
        """
        if _is_wechat_id(chat_id):
            return False
        return chat_id.startswith("oc_") or chat_id.startswith("ou_")

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self, loop, on_message: OnMessageCallback) -> None:
        """Start the underlying WebSocket listener.

        The Channel protocol callback is ``(content, meta)``; the legacy
        FeishuListener hands us ``(content, request_id, meta)``. We adapt
        here — injecting request_id into meta — so tool handlers can continue
        to read ``meta['request_id']`` unchanged.

        Card actions also go through ``on_message`` with
        ``meta['type'] == 'card_action'``; callers (the server / tool layer)
        can branch on that key — exactly as the legacy code did.
        """

        async def _on_msg_shim(content: str, request_id: str, meta: dict) -> None:
            # Ensure request_id is in meta (legacy callers read it from there).
            meta.setdefault("request_id", request_id)
            await on_message(content, meta)

        async def _on_card_shim(content: str, meta: dict) -> None:
            # meta already carries request_id from _parse_card_action
            await on_message(content, meta)

        self._listener = FeishuListener(
            app_id=self._app_id,
            app_secret=self._app_secret,
            allowed_user_ids=self._allowed_user_ids,
            on_message=_on_msg_shim,
            on_card_action=_on_card_shim,
            token_provider=self._token,
            http=self._http,
        )
        self._listener.start(loop)

    async def stop(self) -> None:
        if self._listener:
            await self._listener.stop()

    # ── Shared helpers ───────────────────────────────────────────

    @property
    def http(self) -> httpx.AsyncClient:
        """Expose the shared httpx client (used by tool handlers in S2)."""
        return self._http

    @property
    def token(self) -> TokenProvider[str]:
        """Expose the shared TokenProvider (used by tool handlers in S2)."""
        return self._token

    # ── send_text (with markdown auto-detect) ────────────────────

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> dict:
        """Send a text message. Auto-detects markdown → ``post`` format."""
        use_post = bool(self._MD_PATTERN.search(text))
        for attempt in range(2):
            try:
                token = await self._token.get()

                if use_post:
                    # Post format: md tag for markdown rendering.
                    clean = re.sub(
                        r'\[([^\]]{1,4})\]',
                        lambda m: m.group(1) if m.group(1) in self._EMOJI_MAP else m.group(0),
                        text,
                    )
                    at_parts = re.split(r'<at id=([^>]+)></at>', clean)
                    elements: list = []
                    for i, part in enumerate(at_parts):
                        if i % 2 == 0:
                            if part:
                                elements.append({"tag": "md", "text": part})
                        else:
                            elements.append({"tag": "at", "user_id": part})
                    if not elements:
                        elements = [{"tag": "md", "text": clean}]
                    post_body = {"zh_cn": {"title": "", "content": [elements]}}
                    msg_type = "post"
                    content = json.dumps(post_body)
                else:
                    # Text format: [送心] renders as native emoji
                    msg_text = re.sub(r'<at id=([^>]+)></at>', r'<at user_id="\1">@user</at>', text)
                    msg_text = re.sub(r':([A-Za-z][A-Za-z0-9_]+):', '', msg_text)
                    msg_type = "text"
                    content = json.dumps({"text": msg_text})

                if reply_to:
                    url = f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_to}/reply"
                    body = {"msg_type": msg_type, "content": content}
                else:
                    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
                    body = {"receive_id": chat_id, "msg_type": msg_type, "content": content}

                resp = await self._http.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
                data = resp.json()
                if attempt == 0 and is_token_error(data):
                    logger.info("Send text: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                if data.get("code") != 0:
                    # TODO(session-2): forward 230002 (not a member) → manage_heartbeat("remove", ...)
                    return {"status": "error", "message": f"Send text failed: {data}"}
                return {"status": "ok"}
            except Exception as e:
                logger.error("Send text failed: %s", e)
                return {"status": "error", "message": f"Send text failed: {e}"}
        return {"status": "error", "message": "Send text failed after retry"}

    # ── send_image ───────────────────────────────────────────────

    async def _upload_image_for_key(self, image_path: str) -> str | None:
        """Upload an image and return its ``image_key`` for reuse in posts."""
        if not os.path.exists(image_path):
            return None
        for attempt in range(2):
            try:
                token = await self._token.get()
                with open(image_path, "rb") as f:
                    resp = await self._http.post(
                        "https://open.feishu.cn/open-apis/im/v1/images",
                        headers={"Authorization": f"Bearer {token}"},
                        data={"image_type": "message"},
                        files={"image": (os.path.basename(image_path), f)},
                    )
                data = resp.json()
                if attempt == 0 and is_token_error(data):
                    logger.info("Image upload: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                if data.get("code") == 0:
                    return data["data"]["image_key"]
            except Exception as e:
                logger.error("Image upload failed: %s", e)
                return None
        return None

    async def send_image(self, chat_id: str, path: str) -> dict:
        """Upload an image and send it as an inline image message."""
        if not os.path.exists(path):
            return {"status": "error", "message": f"File not found: {path}"}
        for attempt in range(2):
            try:
                token = await self._token.get()
                with open(path, "rb") as f:
                    resp = await self._http.post(
                        "https://open.feishu.cn/open-apis/im/v1/images",
                        headers={"Authorization": f"Bearer {token}"},
                        data={"image_type": "message"},
                        files={"image": (os.path.basename(path), f)},
                    )
                upload_data = resp.json()
                if attempt == 0 and is_token_error(upload_data):
                    logger.info("Image upload: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                if upload_data.get("code") != 0:
                    return {"status": "error", "message": f"Image upload failed: {upload_data}"}
                image_key = upload_data["data"]["image_key"]

                resp = await self._http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": chat_id,
                        "msg_type": "image",
                        "content": json.dumps({"image_key": image_key}),
                    },
                )
                send_data = resp.json()
                if attempt == 0 and is_token_error(send_data):
                    logger.info("Image send: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                if send_data.get("code") != 0:
                    return {"status": "error", "message": f"Image send failed: {send_data}"}
                return {"status": "ok", "image_key": image_key}
            except Exception as e:
                logger.error("Image reply failed: %s", e)
                return {"status": "error", "message": f"Image reply failed: {e}"}
        return {"status": "error", "message": "Image reply failed after retry"}

    # ── send_file ────────────────────────────────────────────────

    async def send_file(self, chat_id: str, path: str) -> dict:
        """Upload a generic file and send it as a file attachment."""
        return await self.card_manager.upload_and_send_file(chat_id, path)

    # ── send_video ───────────────────────────────────────────────

    async def _upload_video_for_keys(
        self, video_path: str
    ) -> tuple[str | None, str | None]:
        """Upload a video + thumbnail, return ``(file_key, image_key)``."""
        if not os.path.exists(video_path):
            return None, None
        for attempt in range(2):
            try:
                token = await self._token.get()

                # Extract thumbnail
                thumb_path = str(
                    self._temp_dir / f"post_video_thumb_{uuid.uuid4().hex[:8]}.jpg"
                )
                _sp.run(
                    ["ffmpeg", "-i", video_path, "-vf", "select=eq(n\\,0)",
                     "-frames:v", "1", thumb_path, "-y"],
                    capture_output=True, timeout=10,
                )

                image_key = None
                if os.path.exists(thumb_path):
                    image_key = await self._upload_image_for_key(thumb_path)

                file_name = os.path.basename(video_path)
                with open(video_path, "rb") as f:
                    resp = await self._http.post(
                        "https://open.feishu.cn/open-apis/im/v1/files",
                        headers={"Authorization": f"Bearer {token}"},
                        data={"file_type": "mp4", "file_name": file_name},
                        files={"file": (file_name, f)},
                    )
                data = resp.json()
                if attempt == 0 and is_token_error(data):
                    logger.info("Video upload for post: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                file_key = data["data"]["file_key"] if data.get("code") == 0 else None
                return file_key, image_key
            except Exception as e:
                logger.error("Video upload for post failed: %s", e)
                return None, None
        return None, None

    async def send_video(self, chat_id: str, path: str) -> dict:
        """Upload a video + auto-generated thumbnail, send as inline media."""
        if not os.path.exists(path):
            return {"status": "error", "message": f"File not found: {path}"}
        for attempt in range(2):
            try:
                token = await self._token.get()

                # Thumbnail
                thumb_path = str(
                    self._temp_dir / f"video_thumb_{uuid.uuid4().hex[:8]}.jpg"
                )
                _sp.run(
                    ["ffmpeg", "-i", path, "-vf", "select=eq(n\\,0)",
                     "-frames:v", "1", thumb_path, "-y"],
                    capture_output=True, timeout=10,
                )

                image_key = ""
                if os.path.exists(thumb_path):
                    with open(thumb_path, "rb") as f:
                        resp = await self._http.post(
                            "https://open.feishu.cn/open-apis/im/v1/images",
                            headers={"Authorization": f"Bearer {token}"},
                            data={"image_type": "message"},
                            files={"image": ("thumb.jpg", f)},
                        )
                    upload_data = resp.json()
                    if attempt == 0 and is_token_error(upload_data):
                        logger.info("Video thumb upload: token expired, refreshing and retrying")
                        self._token.invalidate()
                        continue
                    if upload_data.get("code") == 0:
                        image_key = upload_data["data"]["image_key"]

                file_name = os.path.basename(path)
                with open(path, "rb") as f:
                    resp = await self._http.post(
                        "https://open.feishu.cn/open-apis/im/v1/files",
                        headers={"Authorization": f"Bearer {token}"},
                        data={"file_type": "mp4", "file_name": file_name},
                        files={"file": (file_name, f)},
                    )
                file_data = resp.json()
                if attempt == 0 and is_token_error(file_data):
                    logger.info("Video file upload: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                if file_data.get("code") != 0:
                    return {"status": "error", "message": f"Video upload failed: {file_data}"}
                file_key = file_data["data"]["file_key"]

                content: dict[str, Any] = {"file_key": file_key}
                if image_key:
                    content["image_key"] = image_key
                resp = await self._http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": chat_id,
                        "msg_type": "media",
                        "content": json.dumps(content),
                    },
                )
                send_data = resp.json()
                if attempt == 0 and is_token_error(send_data):
                    logger.info("Video send: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                if send_data.get("code") != 0:
                    return {"status": "error", "message": f"Video send failed: {send_data}"}
                return {"status": "ok", "file_key": file_key}
            except Exception as e:
                logger.error("Video reply failed: %s", e)
                return {"status": "error", "message": f"Video reply failed: {e}"}
        return {"status": "error", "message": "Video reply failed after retry"}

    # ── send_audio_tts ───────────────────────────────────────────

    async def send_audio_tts(self, chat_id: str, text: str) -> dict:
        """Convert text to speech via ElevenLabs, send as native Feishu audio."""
        if not self._elevenlabs_api_key or not self._elevenlabs_voice_id:
            return {
                "status": "error",
                "message": "ElevenLabs not configured (need ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID)",
            }
        try:
            opus_path = await text_to_speech(
                self._http,
                self._elevenlabs_api_key,
                self._elevenlabs_voice_id,
                text,
                self._temp_dir,
            )
            return await self.card_manager.upload_and_send_audio(chat_id, str(opus_path))
        except Exception as e:
            logger.error("TTS failed: %s", e)
            return {"status": "error", "message": f"TTS failed: {e}"}

    # ── send_post ────────────────────────────────────────────────

    async def send_post(self, chat_id: str, title: str, content: list) -> dict:
        """Send a rich text post (mixed text/image/video/link/at elements).

        Local ``image_path`` / ``video_path`` elements are auto-uploaded to
        get image_key / file_key.
        """
        for attempt in range(2):
            try:
                token = await self._token.get()
                # Auto-upload local files
                for line in content:
                    for elem in line:
                        if elem.get("tag") == "img" and "image_path" in elem and "image_key" not in elem:
                            key = await self._upload_image_for_key(elem["image_path"])
                            if key:
                                elem["image_key"] = key
                            elem.pop("image_path", None)
                        if elem.get("tag") == "media" and "video_path" in elem and "file_key" not in elem:
                            file_key, image_key = await self._upload_video_for_keys(elem["video_path"])
                            if file_key:
                                elem["file_key"] = file_key
                            if image_key:
                                elem["image_key"] = image_key
                            elem.pop("video_path", None)
                post_body = {"zh_cn": {"title": title, "content": content}}
                resp = await self._http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": chat_id,
                        "msg_type": "post",
                        "content": json.dumps(post_body),
                    },
                )
                data = resp.json()
                if attempt == 0 and is_token_error(data):
                    logger.info("Post reply: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                if data.get("code") != 0:
                    return {"status": "error", "message": f"Post send failed: {data}"}
                return {"status": "ok"}
            except Exception as e:
                logger.error("Post reply failed: %s", e)
                return {"status": "error", "message": f"Post reply failed: {e}"}
        return {"status": "error", "message": "Post reply failed after retry"}

    # ── send_reaction ────────────────────────────────────────────

    async def send_reaction(self, message_id: str, emoji: str) -> dict:
        """Send an emoji reaction to a message."""
        for attempt in range(2):
            try:
                token = await self._token.get()
                resp = await self._http.post(
                    f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}/reactions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"reaction_type": {"emoji_type": emoji}},
                )
                data = resp.json()
                if attempt == 0 and is_token_error(data):
                    logger.info("Send reaction: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                if data.get("code") == 0:
                    return {"status": "ok"}
                return {"status": "error", "message": f"Send reaction failed: {data.get('msg', '')}"}
            except Exception as e:
                return {"status": "error", "message": f"Send reaction error: {e}"}
        return {"status": "error", "message": "Send reaction failed after retry"}

    # ── read_history ─────────────────────────────────────────────

    async def read_history(self, chat_id: str, count: int) -> list[dict]:
        """Return up to ``count`` recent messages as simplified dicts."""
        for attempt in range(2):
            try:
                count = min(count, 50)
                token = await self._token.get()
                resp = await self._http.get(
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
                if attempt == 0 and is_token_error(data):
                    logger.info("Read messages: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                if data.get("code") != 0:
                    return []

                items = data.get("data", {}).get("items", [])
                messages: list[dict] = []
                for item in items:
                    sender = item.get("sender", {})
                    body = item.get("body", {}).get("content", "")
                    msg_type = item.get("msg_type", "text")

                    text = ""
                    try:
                        content_json = json.loads(body)
                        if msg_type == "text":
                            text = content_json.get("text", "")
                        elif msg_type == "interactive":
                            texts = []
                            for row in content_json.get("elements", []):
                                if isinstance(row, list):
                                    for el in row:
                                        if isinstance(el, dict) and el.get("tag") == "text" and el.get("text"):
                                            texts.append(el["text"])
                            title = content_json.get("title", "")
                            text = (title + " " + " ".join(texts)).strip() if texts or title else "[card]"
                        elif msg_type == "post":
                            content_json = _unwrap_post_body(content_json)
                            texts = []
                            title = content_json.get("title", "")
                            if title:
                                texts.append(title)
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

                    # Skip bot's own bare cards
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

                return messages
            except Exception as e:
                logger.error("Read messages error: %s", e)
                return []
        return []

    # ── Media download helpers (used by Session 2 tool layer) ────

    async def download_media(
        self,
        content_json: str,
        message_type: str,
        message_id: str = "",
    ) -> tuple[str, Path | None]:
        """Download inbound media by message_type, returning (description, path)."""
        try:
            data = json.loads(content_json)
            token = await self._token.get()
            msg_id = message_id or data.get("message_id", "")

            if message_type == "image":
                path = await download_image(self._http, token, msg_id, data["image_key"], self._temp_dir)
                return f"[Image downloaded to {path}]", path
            elif message_type == "audio":
                path = await download_audio(self._http, token, msg_id, data["file_key"], self._temp_dir)
                transcript = ""
                if self._elevenlabs_api_key:
                    try:
                        transcript = await transcribe_audio(
                            self._http, self._elevenlabs_api_key, path
                        )
                    except Exception as e:
                        logger.warning("Audio transcription failed: %s", e)
                if transcript:
                    return f"[Voice message transcription: {transcript}]", path
                return f"[Audio file downloaded to {path}]", path
            elif message_type == "file":
                path = await download_file(
                    self._http, token, msg_id, data["file_key"],
                    data.get("file_name", ""), self._temp_dir,
                )
                return f"[File downloaded to {path}]", path
            elif message_type == "media":
                import time as _time
                raw_name = data.get("file_name", "video.mp4")
                stem = Path(raw_name).stem
                suffix = Path(raw_name).suffix or ".mp4"
                file_name = f"{stem}_{int(_time.time() * 1000)}{suffix}"
                path = await download_file(
                    self._http, token, msg_id, data["file_key"], file_name, self._temp_dir,
                )
                return f"[Video downloaded to {path}]", path
        except Exception as e:
            logger.error("Media download failed: %s", e)
            return f"[Media download failed: {e}]", None
        return content_json, None
