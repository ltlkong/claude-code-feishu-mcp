"""WeChat adapter — multi-account aware Channel implementation.

State preservation:

* Loads all accounts from ``{state_dir}/wechat_accounts/{id}_auth.json`` plus
  a legacy ``{state_dir}/wechat_auth.json`` migrated as ``account_id='default'``.
* Keeps a ``user_id -> ILinkClient`` map; the *first* authed client is also
  exposed as ``self.default_client`` for send operations that don't specify
  a target account (matches the legacy behavior in
  ``feishu_channel/server.py::_load_wechat_accounts``).
* Each account runs its own :class:`WeChatListener`; listeners are started
  in :meth:`start`.

Send methods route by ``user_id`` via :meth:`_get_client_for` — exact
port of the legacy ``_get_wechat_client`` heuristic (direct match, then
context-token lookup, then fallback to default).
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from ...core.channel import Capabilities, OnMessageCallback
from .auth import login as ilink_login
from .ilink import ILinkClient, MSG_FILE, MSG_IMAGE, MSG_VIDEO, MSG_VOICE
from .listener import WeChatListener
from .media import UPLOAD_FILE, UPLOAD_IMAGE, UPLOAD_VIDEO, download_media, upload_media

logger = logging.getLogger(__name__)


def _is_wechat_id(chat_id: str) -> bool:
    """Identify WeChat chat_ids — all end in ``@im.wechat``."""
    return chat_id.endswith("@im.wechat")


class WeChatChannel:
    """Multi-account WeChat adapter.

    A freshly constructed instance discovers and loads all authed accounts
    from disk. Channels implementations without active credentials behave
    as no-ops: ``owns`` still returns True for WeChat chat_ids, but
    ``send_*`` returns ``{"status": "error", "message": "WeChat not connected"}``.
    """

    id = "wechat"
    capabilities = Capabilities(
        has_cards=False,
        has_reactions=False,
        has_audio=False,
        has_video=True,
        has_post=False,
        has_reply_to=False,
        has_read_history_api=False,  # history comes from local jsonl log
    )

    def __init__(
        self,
        ilink_base_url: str = "https://ilinkai.weixin.qq.com",
        ilink_cdn_url: str = "https://novac2c.cdn.weixin.qq.com/c2c",
        state_dir: Path = Path("workspace/state"),
        wechat_temp_dir: Path = Path("/tmp/wechat-channel"),
    ) -> None:
        self._base_url = ilink_base_url
        self._cdn_url = ilink_cdn_url
        self._state_dir = state_dir
        self._temp_dir = wechat_temp_dir
        self._temp_dir.mkdir(parents=True, exist_ok=True)

        # user_id -> ILinkClient
        self.clients: dict[str, ILinkClient] = {}
        # Parallel list so iteration order matches registration
        self.listeners: list[WeChatListener] = []
        # First authed client — used as default for outbound sends without
        # a pre-identified account (matches legacy behavior)
        self.default_client: ILinkClient | None = None

        self._on_message_cb: OnMessageCallback | None = None
        self._listener_tasks: list[asyncio.Task] = []

        self._load_accounts()

    # ── Account loading ─────────────────────────────────────────

    def _load_accounts(self) -> None:
        """Discover and instantiate authed WeChat accounts on disk."""
        accounts_dir = self._state_dir / "wechat_accounts"

        account_ids: set[str] = set()
        if accounts_dir.is_dir():
            for f in accounts_dir.glob("*_auth.json"):
                account_ids.add(f.name.replace("_auth.json", ""))

        # Legacy single-account migration
        legacy = self._state_dir / "wechat_auth.json"
        if legacy.is_file() and "default" not in account_ids:
            account_ids.add("default")

        for aid in sorted(account_ids):
            client = ILinkClient(
                base_url=self._base_url,
                cdn_url=self._cdn_url,
                state_dir=self._state_dir,
                account_id=aid,
            )
            if client.is_authed and client.user_id:
                self.clients[client.user_id] = client
                if self.default_client is None:
                    self.default_client = client
                logger.info(
                    "WeChat account [%s]: user=%s", aid, client.user_id[:20]
                )

        if self.clients:
            logger.info("WeChat: %d account(s) loaded", len(self.clients))
        else:
            logger.info("WeChat: no authenticated accounts")

    # ── Ownership ────────────────────────────────────────────────

    def owns(self, chat_id: str) -> bool:
        return _is_wechat_id(chat_id)

    # ── Lifecycle ────────────────────────────────────────────────

    async def start(self, loop, on_message: OnMessageCallback) -> None:
        """Start a :class:`WeChatListener` for every authed account.

        Each listener wraps the user callback with a shim that pops the
        ``_wechat_media_item`` field, downloads the media via the adapter,
        and rewrites ``content`` with the local path — matching the legacy
        ``_on_wechat_message`` flow in server.py.
        """
        self._on_message_cb = on_message

        for user_id, client in self.clients.items():
            listener = WeChatListener(
                client=client,
                on_message=self._make_listener_shim(client),
            )
            self.listeners.append(listener)
            task = asyncio.create_task(listener.start())
            task.add_done_callback(self._on_listener_done)
            self._listener_tasks.append(task)

        if self.listeners:
            logger.info("WeChat: %d listener(s) started", len(self.listeners))

    def _make_listener_shim(
        self, client: ILinkClient
    ) -> Callable[[str, dict[str, Any]], Awaitable[None]]:
        """Build the per-account on_message shim.

        The shim:
          1) Pops the raw ``_wechat_media_item`` (if any) from meta.
          2) Downloads the media via this account's client.
          3) Rewrites ``content`` to reference the local path (so tools can
             read/forward it).
          4) Forwards to the registry-level callback.
        """

        async def shim(content: str, meta: dict[str, Any]) -> None:
            media_item = meta.pop("_wechat_media_item", None)
            if media_item:
                try:
                    item_type = media_item.get("type", 0)
                    media_info = None
                    if item_type == MSG_IMAGE:
                        media_info = media_item.get("image_item", {}).get("media", {})
                    elif item_type == MSG_VOICE:
                        media_info = media_item.get("voice_item", {}).get("media", {})
                    elif item_type == MSG_FILE:
                        media_info = media_item.get("file_item", {}).get("media", {})
                    elif item_type == MSG_VIDEO:
                        media_info = media_item.get("video_item", {}).get("media", {})

                    if media_info and media_info.get("encrypt_query_param"):
                        media_info["type"] = item_type
                        local_path = await download_media(
                            client, media_info, self._temp_dir
                        )
                        content = f"[{meta.get('message_type', 'Media').title()} downloaded to {local_path}]"
                        logger.info("WeChat media downloaded: %s", local_path)
                except Exception as e:
                    logger.error("WeChat media download failed: %s", e, exc_info=True)

            if self._on_message_cb:
                await self._on_message_cb(content, meta)

        return shim

    def _on_listener_done(self, task: asyncio.Task) -> None:
        """Log when a listener task ends unexpectedly."""
        try:
            exc = task.exception()
            if exc:
                logger.error(
                    "WeChat listener task died with exception: %s",
                    exc, exc_info=exc,
                )
            else:
                logger.warning("WeChat listener task ended cleanly (unexpected)")
        except asyncio.CancelledError:
            logger.info("WeChat listener task was cancelled")

    async def stop(self) -> None:
        """Cancel all listener tasks and close ILink clients."""
        for listener in self.listeners:
            listener.stop()
        for task in self._listener_tasks:
            task.cancel()
        for client in self.clients.values():
            try:
                await client.close()
            except Exception:
                pass

    # ── Account routing ──────────────────────────────────────────

    def _get_client_for(self, user_id: str) -> ILinkClient | None:
        """Find the right WeChat client for a target user_id.

        Mirrors the legacy ``_get_wechat_client`` heuristic:
          1) Direct match against an account's own user_id
          2) Scan for a client that already holds a context_token for the user
          3) Fallback to the default (first authed) client
        """
        if user_id in self.clients:
            return self.clients[user_id]
        for client in self.clients.values():
            if client.get_context_token(user_id):
                return client
        return self.default_client

    # ── Send — required Channel API ──────────────────────────────

    async def send_text(
        self, chat_id: str, text: str, reply_to: str | None = None
    ) -> dict:
        """Send text. WeChat has no native reply_to — we ignore that arg."""
        client = self._get_client_for(chat_id)
        if not client:
            return {"status": "error", "message": "WeChat not connected"}
        try:
            if len(text) > 4000:
                text = text[:3997] + "..."
            await client.send_text(chat_id, text)
            return {"status": "ok"}
        except Exception as e:
            logger.error("WeChat send_text failed: %s", e)
            return {"status": "error", "message": str(e)}

    async def send_image(self, chat_id: str, path: str) -> dict:
        """Encrypt + upload image to WeChat CDN, then send image message."""
        client = self._get_client_for(chat_id)
        if not client:
            return {"status": "error", "message": "WeChat not connected"}
        try:
            p = Path(path)
            if not p.is_file():
                return {"status": "error", "message": f"File not found: {path}"}
            cdn_ref = await upload_media(client, p, UPLOAD_IMAGE, to_user_id=chat_id)
            aes_key_b64 = _b64.b64encode(cdn_ref["aes_key"].encode()).decode()
            await client.send_message(chat_id, [{
                "type": MSG_IMAGE,
                "image_item": {
                    "media": {
                        "encrypt_query_param": cdn_ref["encrypt_query_param"],
                        "aes_key": aes_key_b64,
                        "encrypt_type": 1,
                    },
                    "mid_size": cdn_ref["cipher_size"],
                },
            }])
            return {"status": "ok"}
        except Exception as e:
            logger.error("WeChat send_image failed: %s", e)
            return {"status": "error", "message": str(e)}

    async def send_file(self, chat_id: str, path: str) -> dict:
        """Encrypt + upload file to WeChat CDN, then send file message."""
        client = self._get_client_for(chat_id)
        if not client:
            return {"status": "error", "message": "WeChat not connected"}
        try:
            p = Path(path)
            if not p.is_file():
                return {"status": "error", "message": f"File not found: {path}"}
            cdn_ref = await upload_media(client, p, UPLOAD_FILE, to_user_id=chat_id)
            aes_key_b64 = _b64.b64encode(cdn_ref["aes_key"].encode()).decode()
            await client.send_message(chat_id, [{
                "type": MSG_FILE,
                "file_item": {
                    "media": {
                        "encrypt_query_param": cdn_ref["encrypt_query_param"],
                        "aes_key": aes_key_b64,
                        "encrypt_type": 1,
                    },
                    "file_name": p.name,
                    "len": str(cdn_ref["raw_size"]),
                },
            }])
            return {"status": "ok"}
        except Exception as e:
            logger.error("WeChat send_file failed: %s", e)
            return {"status": "error", "message": str(e)}

    # ── Capability-gated — not supported by WeChat ──────────────

    async def send_video(self, chat_id: str, path: str) -> dict:
        """Encrypt + upload video to WeChat CDN, then send video message."""
        client = self._get_client_for(chat_id)
        if not client:
            return {"status": "error", "message": "WeChat not connected"}
        try:
            p = Path(path)
            if not p.is_file():
                return {"status": "error", "message": f"File not found: {path}"}
            cdn_ref = await upload_media(client, p, UPLOAD_VIDEO, to_user_id=chat_id)
            aes_key_b64 = _b64.b64encode(cdn_ref["aes_key"].encode()).decode()
            await client.send_message(chat_id, [{
                "type": MSG_VIDEO,
                "video_item": {
                    "media": {
                        "encrypt_query_param": cdn_ref["encrypt_query_param"],
                        "aes_key": aes_key_b64,
                        "encrypt_type": 1,
                    },
                    "file_name": p.name,
                    "len": str(cdn_ref["raw_size"]),
                    "video_size": cdn_ref["cipher_size"],
                },
            }])
            return {"status": "ok"}
        except Exception as e:
            logger.error("WeChat send_video failed: %s", e)
            return {"status": "error", "message": str(e)}

    async def send_audio_tts(self, chat_id: str, text: str) -> dict:
        return {"status": "error", "message": "send_audio_tts not supported by WeChat"}

    async def send_post(self, chat_id: str, title: str, content: list) -> dict:
        return {"status": "error", "message": "send_post not supported by WeChat"}

    async def send_reaction(self, message_id: str, emoji: str) -> dict:
        return {"status": "error", "message": "send_reaction not supported by WeChat"}

    async def read_history(self, chat_id: str, count: int) -> list[dict]:
        """WeChat history comes from the local jsonl log (Session 2 wires it)."""
        return []

    # ── Session-2 helpers ────────────────────────────────────────

    async def login_new_account(self, account_id: str = "new") -> ILinkClient | None:
        """Interactive QR login for a new account. Used by ``wechat_login_qr`` tool.

        The caller is responsible for polling the returned client's
        ``poll_qr_status`` and, on confirmation, calling
        :meth:`register_logged_in_client` to start its listener.
        """
        client = ILinkClient(
            base_url=self._base_url,
            cdn_url=self._cdn_url,
            state_dir=self._state_dir,
            account_id=account_id,
        )
        ok = await ilink_login(client)
        if not ok:
            await client.close()
            return None
        self.register_logged_in_client(client)
        return client

    def register_logged_in_client(self, client: ILinkClient) -> WeChatListener:
        """Add a freshly-authed client + start its listener immediately."""
        self.clients[client.user_id] = client
        if self.default_client is None:
            self.default_client = client
        listener = WeChatListener(
            client=client,
            on_message=self._make_listener_shim(client),
        )
        self.listeners.append(listener)
        if self._on_message_cb:  # only start immediately if channel is running
            task = asyncio.create_task(listener.start())
            task.add_done_callback(self._on_listener_done)
            self._listener_tasks.append(task)
        return listener
