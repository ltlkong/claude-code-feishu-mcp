"""iLink HTTP API client for WeChat bot communication.

Ported verbatim from ``wechat_channel/ilink.py``. Wraps Tencent's iLink API
endpoints for QR login, message polling, and message sending. All media
goes through CDN with AES-128-ECB encryption.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Message type constants
MSG_TEXT = 1
MSG_IMAGE = 2
MSG_VOICE = 3
MSG_FILE = 4
MSG_VIDEO = 5

# QR code status
QR_WAIT = "wait"
QR_SCANNED = "scaned"
QR_CONFIRMED = "confirmed"
QR_REDIRECT = "scaned_but_redirect"
QR_EXPIRED = "expired"


class ILinkProtocolError(RuntimeError):
    """Raised when iLink returns an application-level error payload."""

    def __init__(self, errcode: int, errmsg: str) -> None:
        super().__init__(f"iLink error {errcode}: {errmsg}")
        self.errcode = errcode
        self.errmsg = errmsg


def _random_uin() -> str:
    """Generate a random X-WECHAT-UIN header value (base64 of random uint32)."""
    val = struct.unpack("I", os.urandom(4))[0]
    return base64.b64encode(str(val).encode()).decode()


class ILinkClient:
    """Async HTTP client for Tencent iLink bot API.

    Each instance represents one WeChat account (one QR scan = one account).
    Multiple instances can run in parallel for multi-account support.
    """

    def __init__(
        self,
        base_url: str,
        cdn_url: str,
        state_dir: Path,
        account_id: str = "default",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.cdn_url = cdn_url.rstrip("/")
        self.state_dir = state_dir
        self.account_id = account_id

        # Auth state
        self.token: str = ""
        self.bot_id: str = ""
        self.user_id: str = ""  # The WeChat user who scanned the QR
        self.uin: str = _random_uin()

        # Sync state
        self.updates_buf: str = ""
        self._context_tokens: dict[str, str] = {}  # user_id -> context_token

        # HTTP client
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0))

        # Load persisted state
        self._load_auth()
        self._load_sync()

    # ── Persistence ──────────────────────────────────────────────

    def _accounts_dir(self) -> Path:
        return self.state_dir / "wechat_accounts"

    def _auth_path(self) -> Path:
        return self._accounts_dir() / f"{self.account_id}_auth.json"

    def _sync_path(self) -> Path:
        return self._accounts_dir() / f"{self.account_id}_sync.json"

    def _load_auth(self) -> None:
        # Try new per-account path first, fall back to legacy single-account
        path = self._auth_path()
        if not path.is_file():
            legacy = self.state_dir / "wechat_auth.json"
            if legacy.is_file() and self.account_id == "default":
                path = legacy
        if path.is_file():
            try:
                data = json.loads(path.read_text())
                self.token = data.get("token", "")
                self.bot_id = data.get("bot_id", "")
                self.user_id = data.get("user_id", "")
                if data.get("base_url"):
                    self.base_url = data["base_url"]
                logger.info(
                    "Loaded WeChat account [%s]: bot_id=%s user=%s",
                    self.account_id,
                    self.bot_id[:12] if self.bot_id else "none",
                    self.user_id[:20] if self.user_id else "none",
                )
            except (json.JSONDecodeError, ValueError):
                pass

    def save_auth(self) -> None:
        self._accounts_dir().mkdir(parents=True, exist_ok=True)
        tmp = self._auth_path().with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "token": self.token,
            "bot_id": self.bot_id,
            "user_id": self.user_id,
            "base_url": self.base_url,
            "account_id": self.account_id,
            "saved_at": time.time(),
        }, indent=2))
        tmp.rename(self._auth_path())

    def _load_sync(self) -> None:
        path = self._sync_path()
        if not path.is_file():
            legacy = self.state_dir / "wechat_sync.json"
            if legacy.is_file() and self.account_id == "default":
                path = legacy
        if path.is_file():
            try:
                data = json.loads(path.read_text())
                self.updates_buf = data.get("buf", "")
                self._context_tokens = data.get("context_tokens", {})
            except (json.JSONDecodeError, ValueError):
                pass

    def save_sync(self) -> None:
        self._accounts_dir().mkdir(parents=True, exist_ok=True)
        tmp = self._sync_path().with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "buf": self.updates_buf,
            "context_tokens": self._context_tokens,
        }, indent=2))
        tmp.rename(self._sync_path())

    # ── Headers ──────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": self.uin,
            "iLink-App-Id": "bot",
            "iLink-App-ClientVersion": "131334",  # 2.1.6 → (2<<16)|(1<<8)|6
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    # ── API methods ──────────────────────────────────────────────

    @property
    def is_authed(self) -> bool:
        return bool(self.token)

    async def get_qr_code(self) -> dict[str, Any]:
        resp = await self._client.get(
            f"{self.base_url}/ilink/bot/get_bot_qrcode",
            params={"bot_type": "3"},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def poll_qr_status(self, qrcode_id: str) -> dict[str, Any]:
        resp = await self._client.get(
            f"{self.base_url}/ilink/bot/get_qrcode_status",
            params={"qrcode": qrcode_id},
            headers=self._headers(),
            timeout=httpx.Timeout(45.0),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_updates(self) -> list[dict]:
        body: dict[str, Any] = {"base_info": {"channel_version": "2.1.6"}}
        if self.updates_buf:
            body["get_updates_buf"] = self.updates_buf

        resp = await self._client.post(
            f"{self.base_url}/ilink/bot/getupdates",
            json=body,
            headers=self._headers(),
            timeout=httpx.Timeout(45.0),
        )
        resp.raise_for_status()
        data = resp.json()
        errcode = data.get("errcode")
        if isinstance(errcode, int) and errcode != 0:
            errmsg = str(data.get("errmsg", "unknown error"))
            raise ILinkProtocolError(errcode, errmsg)

        msgs_count = len(data.get("msgs", []))
        if msgs_count > 0:
            logger.info("getupdates: msgs=%d ret=%s", msgs_count, data.get("ret"))
        elif not data.get("get_updates_buf"):
            logger.debug(
                "getupdates missing buffer: keys=%s ret=%s",
                list(data.keys()), data.get("ret"),
            )

        if data.get("get_updates_buf"):
            self.updates_buf = data["get_updates_buf"]

        messages = data.get("msgs", [])
        for msg in messages:
            from_user = msg.get("from_user_id", "")
            ctx = msg.get("context_token", "")
            if from_user and ctx:
                self._context_tokens[from_user] = ctx

        self.save_sync()
        return messages

    def get_context_token(self, user_id: str) -> str:
        return self._context_tokens.get(user_id, "")

    async def send_message(
        self, to_user: str, item_list: list[dict], context_token: str = ""
    ) -> dict:
        ctx = context_token or self.get_context_token(to_user)
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user,
                "client_id": f"xiaobai-{uuid.uuid4().hex[:16]}",
                "message_type": 2,   # BOT
                "message_state": 2,  # FINISH
                "item_list": item_list,
            },
            "base_info": {"channel_version": "2.1.6"},
        }
        if ctx:
            body["msg"]["context_token"] = ctx

        resp = await self._client.post(
            f"{self.base_url}/ilink/bot/sendmessage",
            json=body,
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def send_text(self, to_user: str, text: str, context_token: str = "") -> dict:
        return await self.send_message(
            to_user,
            [{"type": MSG_TEXT, "text_item": {"text": text}}],
            context_token=context_token,
        )

    async def send_typing(self, to_user: str, cancel: bool = False) -> dict:
        body = {
            "ilink_user_id": to_user,
            "status": 2 if cancel else 1,
            "base_info": {"channel_version": "2.1.6"},
        }
        ctx = self.get_context_token(to_user)
        if ctx:
            body["context_token"] = ctx

        resp = await self._client.post(
            f"{self.base_url}/ilink/bot/sendtyping",
            json=body,
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_upload_url(
        self,
        filekey: str,
        media_type: int,
        raw_size: int,
        raw_md5: str,
        cipher_size: int,
        aes_key_hex: str,
        to_user_id: str = "",
        no_need_thumb: bool = True,
        **kwargs,
    ) -> dict:
        body = {
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to_user_id,
            "rawsize": raw_size,
            "rawfilemd5": raw_md5,
            "filesize": cipher_size,
            "no_need_thumb": no_need_thumb,
            "aeskey": aes_key_hex,
            "base_info": {"channel_version": "2.1.6"},
            **kwargs,
        }
        resp = await self._client.post(
            f"{self.base_url}/ilink/bot/getuploadurl",
            json=body,
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def get_config(self) -> dict:
        resp = await self._client.post(
            f"{self.base_url}/ilink/bot/getconfig",
            json={},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    async def close(self) -> None:
        await self._client.aclose()
