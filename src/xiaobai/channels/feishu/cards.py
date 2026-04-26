"""Card manager for two-section Feishu cards (action + response text).

Ported from ``feishu_channel/card.py``. The only behavior change is that
token management is delegated to a shared :class:`TokenProvider` instead of
CardManager's own cache, so the listener and card manager share refreshes.

Card layout::

    ┌──────────────────────────────┐
    │ {action_text}                │  <- card header (streaming state)
    ├──────────────────────────────┤
    │ {response_text}              │  <- card body (grows over time)
    └──────────────────────────────┘
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ...core.auth import TokenProvider

logger = logging.getLogger(__name__)


def _build_card_json(
    action: str,
    text: str,
    streaming: bool = True,
    emoji: str = "",
    template: str = "indigo",
) -> str:
    """Build a v2 (CardKit) card JSON with header and content.

    When ``action`` is non-empty the card gets a coloured header; otherwise
    (finalized state) no header is rendered.
    """
    elements = []

    # Fix literal "\n" sequences that arrive from MCP tool calls
    if text:
        text = text.replace("\\n", "\n")

    elements.append({
        "tag": "markdown",
        "content": text or "...",
        "element_id": "content",
    })

    card: dict = {
        "schema": "2.0",
        "config": {"streaming_mode": streaming},
        "body": {"elements": elements},
    }

    if action:
        header_text = f"{emoji} {action}" if emoji else action
        card["header"] = {
            "title": {"tag": "plain_text", "content": header_text},
            "template": template,
        }

    return json.dumps(card, ensure_ascii=False)


def _flatten_action_tags(elements: list) -> list:
    """Flatten V1 ``action`` wrapper tags into direct elements for V2.

    V1: ``{"tag": "action", "actions": [{"tag": "button", ...}, ...]}``
    V2: ``{"tag": "button", ...}, {"tag": "button", ...}``
    """
    result = []
    for el in elements:
        if el.get("tag") == "action" and "actions" in el:
            result.extend(el["actions"])
        else:
            result.append(el)
    return result


@dataclass
class CardState:
    """Tracks a single active card."""

    chat_id: str
    reply_to_message_id: str
    message_id: str | None = None   # Feishu message ID (set after card is created)
    card_id: str | None = None      # CardKit card ID
    sequence: int = 0
    created_at: float = field(default_factory=time.time)


class CardManager:
    """Creates and updates two-section Feishu cards.

    Uses CardKit API (streaming mode) as primary, PATCH as fallback. All
    token handling is delegated to ``token_provider`` so multiple consumers
    share refreshes.
    """

    def __init__(
        self,
        token_provider: TokenProvider[str],
        http: httpx.AsyncClient,
        stale_timeout_minutes: int = 30,
        persist_path: Path | None = None,
    ) -> None:
        self._http = http
        self._token = token_provider
        self._stale_timeout = stale_timeout_minutes * 60
        # request_id -> CardState
        self._cards: dict[str, CardState] = {}
        # request_id -> (chat_id, reply_to_message_id) for deferred card creation
        self._pending: dict[str, tuple[str, str]] = {}
        # request_id -> (chat_id, reply_to_message_id) — persists after
        # finalize, enabling auto-recovery if reply_card is called again.
        self._origins: dict[str, tuple[str, str]] = {}

        # Cross-restart persistence so cards left mid-stream by a crashed bot
        # can be finalized on the next boot (otherwise the Feishu client shows
        # ☁️…… forever).
        self._persist_path = persist_path
        self._load()

    # ── Cross-restart persistence ────────────────────────────────

    def _load(self) -> None:
        """Restore in-flight card state from disk (best-effort)."""
        if not self._persist_path or not self._persist_path.is_file():
            return
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            for rid, info in (data.get("cards") or {}).items():
                self._cards[rid] = CardState(
                    chat_id=info["chat_id"],
                    reply_to_message_id=info["reply_to_message_id"],
                    message_id=info.get("message_id"),
                    card_id=info.get("card_id"),
                    sequence=int(info.get("sequence") or 0),
                    created_at=float(info.get("created_at") or time.time()),
                )
            for rid, origin in (data.get("origins") or {}).items():
                if isinstance(origin, list) and len(origin) == 2:
                    self._origins[rid] = (origin[0], origin[1])
            logger.info(
                "CardManager loaded %d in-flight + %d origins from %s",
                len(self._cards), len(self._origins), self._persist_path,
            )
        except Exception as e:
            logger.warning("CardManager load failed: %s", e)

    def _persist(self) -> None:
        """Snapshot in-flight card state to disk (atomic write)."""
        if not self._persist_path:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "cards": {
                    rid: {
                        "chat_id": s.chat_id,
                        "reply_to_message_id": s.reply_to_message_id,
                        "message_id": s.message_id,
                        "card_id": s.card_id,
                        "sequence": s.sequence,
                        "created_at": s.created_at,
                    }
                    for rid, s in self._cards.items()
                },
                "origins": {rid: list(o) for rid, o in self._origins.items()},
            }
            tmp = self._persist_path.with_suffix(self._persist_path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._persist_path)
        except Exception as e:
            logger.warning("CardManager persist failed: %s", e)

    async def recover_in_flight(self) -> int:
        """Finalize cards left in-flight from a previous run.

        Called once at startup. Each card gets a polite '[recovered]' finalize
        so the user sees a stable, non-spinning card instead of ☁️……
        """
        if not self._cards:
            return 0
        rids = list(self._cards.keys())
        recovered = 0
        for rid in rids:
            try:
                await self.finalize_card(
                    rid,
                    "*[Recovered after restart — original response was lost]*",
                )
                recovered += 1
            except Exception as e:
                logger.warning("recover_in_flight: failed to finalize %s: %s", rid, e)
                self._cards.pop(rid, None)
        self._persist()
        if recovered:
            logger.info("CardManager recovered %d stuck card(s) from previous run", recovered)
        return recovered

    # ── Token error helpers ──────────────────────────────────────

    @staticmethod
    def _is_token_error(data: dict) -> bool:
        return data.get("code") in (99991663, 99991664, 99991668)

    async def _headers(self) -> dict[str, str]:
        token = await self._token.get()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # ── CardKit API ──────────────────────────────────────────────

    async def _cardkit_create(self, card_json: str) -> str | None:
        """Create a card via CardKit API, return card_id or None on failure."""
        for attempt in range(2):
            try:
                headers = await self._headers()
                resp = await self._http.post(
                    "https://open.feishu.cn/open-apis/cardkit/v1/cards",
                    headers=headers,
                    json={"type": "card_json", "data": card_json},
                )
                data = resp.json()
                if data.get("code") == 0:
                    return data["data"]["card_id"]
                if attempt == 0 and self._is_token_error(data):
                    logger.info("CardKit create: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                logger.warning("CardKit create failed: %s", data)
            except Exception as e:
                logger.warning("CardKit create error: %s", e)
            break
        return None

    async def _cardkit_replace(self, card_id: str, card_json: str, seq: int) -> bool:
        """Replace an entire CardKit card with new content."""
        for attempt in range(2):
            try:
                headers = await self._headers()
                body = {"card": {"type": "card_json", "data": card_json}, "sequence": seq}
                logger.debug(
                    "CardKit replace: card_id=%s seq=%d body_size=%d",
                    card_id, seq, len(json.dumps(body)),
                )
                resp = await self._http.put(
                    f"https://open.feishu.cn/open-apis/cardkit/v1/cards/{card_id}",
                    headers=headers,
                    json=body,
                )
                data = resp.json()
                logger.debug("CardKit replace response: %s", json.dumps(data, ensure_ascii=False)[:500])
                if data.get("code") == 0:
                    return True
                if attempt == 0 and self._is_token_error(data):
                    logger.info("CardKit replace: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                logger.warning("CardKit replace failed: code=%s msg=%s", data.get("code"), data.get("msg"))
            except Exception as e:
                logger.warning("CardKit replace error: %s", e)
            break
        return False

    async def _cardkit_finalize(self, card_id: str, seq: int) -> None:
        """Turn off streaming mode on a CardKit card."""
        for attempt in range(2):
            try:
                headers = await self._headers()
                resp = await self._http.patch(
                    f"https://open.feishu.cn/open-apis/cardkit/v1/cards/{card_id}/settings",
                    headers=headers,
                    json={
                        "settings": json.dumps({"streaming_mode": False}),
                        "sequence": seq,
                    },
                )
                data = resp.json()
                if attempt == 0 and self._is_token_error(data):
                    logger.info("CardKit finalize: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                return
            except Exception as e:
                logger.warning("CardKit finalize error: %s", e)
            break

    # ── Feishu IM API ────────────────────────────────────────────

    async def _send_card_message(
        self,
        chat_id: str,
        reply_to: str,
        card_json: str,
        card_id: str | None,
    ) -> str | None:
        """Send a card message via IM API. Returns message_id or None."""
        if card_id:
            content = json.dumps({"type": "card", "data": {"card_id": card_id}})
        else:
            content = card_json

        for attempt in range(2):
            headers = await self._headers()
            body = {
                "receive_id": chat_id,
                "msg_type": "interactive",
                "content": content,
            }
            try:
                resp = await self._http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers=headers,
                    json=body,
                )
                data = resp.json()
                if data.get("code") == 0:
                    return data["data"]["message_id"]
                if attempt == 0 and self._is_token_error(data):
                    logger.info("Send card: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                # Try as reply if direct send fails
                resp = await self._http.post(
                    f"https://open.feishu.cn/open-apis/im/v1/messages/{reply_to}/reply",
                    headers=headers,
                    json={"msg_type": "interactive", "content": content},
                )
                data = resp.json()
                if data.get("code") == 0:
                    return data["data"]["message_id"]
                if attempt == 0 and self._is_token_error(data):
                    logger.info("Send card reply: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                logger.warning("Send card failed: %s", data)
            except Exception as e:
                logger.warning("Send card error: %s", e)
            break
        return None

    async def _patch_card(self, message_id: str, card_json: str) -> bool:
        """PATCH update an existing card message."""
        for attempt in range(2):
            try:
                headers = await self._headers()
                resp = await self._http.patch(
                    f"https://open.feishu.cn/open-apis/im/v1/messages/{message_id}",
                    headers=headers,
                    json={"content": card_json},
                )
                data = resp.json()
                if data.get("code") == 0:
                    return True
                if attempt == 0 and self._is_token_error(data):
                    logger.info("Patch card: token expired, refreshing and retrying")
                    self._token.invalidate()
                    continue
                logger.warning("Patch card failed: %s", data)
            except Exception as e:
                logger.warning("Patch card error: %s", e)
            break
        return False

    # ── File upload & send ───────────────────────────────────────

    async def _upload_and_send(
        self,
        chat_id: str,
        file_path: str,
        file_type: str,
        msg_type: str,
        mime: str | None = None,
    ) -> dict:
        """Upload a file to Feishu and send it as a message.

        Args:
            file_type: Feishu upload type ("stream" for files, "opus" for audio)
            msg_type: Feishu message type ("file" or "audio")
            mime: Optional MIME type override for the upload
        """
        path = Path(file_path)
        if not path.exists():
            return {"status": "error", "message": f"File not found: {file_path}"}

        for attempt in range(2):
            try:
                token = await self._token.get()
                file_tuple = (
                    (path.name, open(path, "rb"), mime) if mime
                    else (path.name, open(path, "rb"))
                )
                try:
                    resp = await self._http.post(
                        "https://open.feishu.cn/open-apis/im/v1/files",
                        headers={"Authorization": f"Bearer {token}"},
                        data={"file_type": file_type, "file_name": path.name},
                        files={"file": file_tuple},
                    )
                finally:
                    file_tuple[1].close()

                data = resp.json()
                if attempt == 0 and self._is_token_error(data):
                    logger.debug("Upload %s: token expired, retrying", msg_type)
                    self._token.invalidate()
                    continue
                if data.get("code") != 0:
                    return {"status": "error", "message": f"Upload failed: {data}"}
                file_key = data["data"]["file_key"]

                headers = await self._headers()
                resp = await self._http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers=headers,
                    json={
                        "receive_id": chat_id,
                        "msg_type": msg_type,
                        "content": json.dumps({"file_key": file_key}),
                    },
                )
                data = resp.json()
                if data.get("code") == 0:
                    return {"status": "ok"}
                if attempt == 0 and self._is_token_error(data):
                    logger.debug("Send %s: token expired, retrying", msg_type)
                    self._token.invalidate()
                    continue
                return {"status": "error", "message": f"Send failed: {data}"}
            except Exception as e:
                return {"status": "error", "message": str(e)}
        return {"status": "error", "message": "Failed after retry"}

    async def upload_and_send_file(self, chat_id: str, file_path: str) -> dict:
        """Upload a file to Feishu and send it as a file message."""
        return await self._upload_and_send(chat_id, file_path, "stream", "file")

    async def upload_and_send_audio(self, chat_id: str, file_path: str) -> dict:
        """Upload an opus file to Feishu and send it as a native audio message."""
        return await self._upload_and_send(chat_id, file_path, "opus", "audio", "audio/ogg")

    # ── Public API ───────────────────────────────────────────────

    async def adopt_card(self, request_id: str, chat_id: str, message_id: str) -> None:
        """Adopt an existing card so update_card/finalize_card can update it."""
        self._cards[request_id] = CardState(
            chat_id=chat_id,
            reply_to_message_id="",
            message_id=message_id,
        )
        self._persist()
        logger.info("adopt_card: request_id=%s message_id=%s", request_id, message_id)

    def register_pending(
        self, request_id: str, chat_id: str, reply_to_message_id: str
    ) -> None:
        """Register a pending card — created lazily when update/finalize fires."""
        self._pending[request_id] = (chat_id, reply_to_message_id)
        self._origins[request_id] = (chat_id, reply_to_message_id)
        self._persist()

    def cancel_pending(self, request_id: str) -> None:
        """Cancel a pending card and clear its origin record."""
        self._pending.pop(request_id, None)
        self._origins.pop(request_id, None)
        self._persist()

    async def _ensure_card(self, request_id: str) -> bool:
        """Ensure a card exists for ``request_id``.

        Tries in order: 1) existing card, 2) pending registration,
        3) auto-recovery from ``_origins`` (when a previous card was lost).
        """
        if request_id in self._cards:
            return True
        pending = self._pending.pop(request_id, None)
        if pending:
            chat_id, reply_to_message_id = pending
            await self.create_card(request_id, chat_id, reply_to_message_id)
            return True
        # Auto-recovery: state is gone but we still know the chat
        origin = self._origins.get(request_id)
        if origin:
            chat_id, reply_to_message_id = origin
            logger.info(
                "_ensure_card: auto-recovering card for request_id=%s in chat=%s",
                request_id, chat_id,
            )
            await self.create_card(request_id, chat_id, reply_to_message_id)
            return True
        return False

    async def create_card(
        self, request_id: str, chat_id: str, reply_to_message_id: str
    ) -> None:
        """Create a new card in 'thinking...' state."""
        card_json = _build_card_json("", "💭...", streaming=True)
        card_id = await self._cardkit_create(card_json)
        message_id = await self._send_card_message(
            chat_id, reply_to_message_id, card_json, card_id
        )

        self._cards[request_id] = CardState(
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            message_id=message_id,
            card_id=card_id,
        )
        self._persist()

    async def update_card(
        self,
        request_id: str,
        status: str,
        text: str,
        emoji: str = "",
        template: str = "indigo",
    ) -> dict:
        """Update card header/body. Auto-recovers if the card was lost."""
        if request_id not in self._cards:
            await self._ensure_card(request_id)
        state = self._cards.get(request_id)
        if not state:
            logger.warning("update_card: no card for request_id %s", request_id)
            return {"status": "error", "message": f"No card for request_id {request_id}"}

        state.sequence += 1
        card_json = _build_card_json(status, text, streaming=True, emoji=emoji, template=template)
        logger.debug(
            "update_card: request_id=%s card_id=%s message_id=%s seq=%d",
            request_id, state.card_id, state.message_id, state.sequence,
        )

        if state.card_id:
            if await self._cardkit_replace(state.card_id, card_json, state.sequence):
                self._persist()
                return {"status": "ok"}
            logger.warning("update_card: CardKit replace failed, trying PATCH fallback")

        if state.message_id:
            if await self._patch_card(state.message_id, card_json):
                self._persist()
                return {"status": "ok"}

        return {"status": "error", "message": "Failed to update card"}

    def _is_card_json(self, text: str) -> bool:
        """Return True if ``text`` is a Feishu V2 card JSON blob."""
        stripped = text.strip()
        if not stripped.startswith("{"):
            return False
        try:
            card = json.loads(stripped)
            return isinstance(card, dict) and card.get("schema") == "2.0"
        except (json.JSONDecodeError, TypeError):
            return False

    async def finalize_card(self, request_id: str, text: str) -> dict:
        """Finalize a card: replace with final content, disable streaming."""
        await self._ensure_card(request_id)
        state = self._cards.get(request_id)
        if not state:
            return {"status": "error", "message": f"No card for request_id {request_id}"}

        state.sequence += 1

        # If Claude sent a V2 card JSON, use it directly. Otherwise wrap it.
        is_card = self._is_card_json(text)
        if is_card:
            card_dict = json.loads(text.strip())
            card_dict.pop("config", None)  # Let CardKit manage config
            # Flatten V1 "action" wrappers — V2 doesn't support them
            if "body" in card_dict and "elements" in card_dict["body"]:
                card_dict["body"]["elements"] = _flatten_action_tags(
                    card_dict["body"]["elements"]
                )
            card_json = json.dumps(card_dict, ensure_ascii=False)
        else:
            card_json = _build_card_json("", text, streaming=False)

        logger.debug(
            "finalize_card: request_id=%s is_card=%s card_id=%s message_id=%s seq=%d card_json_len=%d",
            request_id, is_card, state.card_id, state.message_id, state.sequence, len(card_json),
        )

        # CardKit replace (primary)
        replaced = False
        if state.card_id:
            replaced = await self._cardkit_replace(state.card_id, card_json, state.sequence)
            if replaced:
                state.sequence += 1
                await self._cardkit_finalize(state.card_id, state.sequence)
            else:
                logger.warning(
                    "finalize_card: CardKit replace failed for card_id=%s, trying PATCH",
                    state.card_id,
                )

        if not replaced and state.message_id:
            replaced = await self._patch_card(state.message_id, card_json)
            logger.debug(
                "finalize_card: PATCH fallback result=%s for message_id=%s",
                replaced, state.message_id,
            )

        if not replaced and not state.card_id and not state.message_id:
            # No card was ever created (e.g. token was expired at creation).
            # Try to create and send a fresh card now.
            logger.debug(
                "finalize_card: no card exists, creating fresh card for request_id=%s",
                request_id,
            )
            card_id = await self._cardkit_create(card_json)
            message_id = await self._send_card_message(
                state.chat_id, state.reply_to_message_id, card_json, card_id
            )
            replaced = message_id is not None

        del self._cards[request_id]
        self._pending.pop(request_id, None)
        # Keep _origins — if reply_card is called again on this request_id,
        # _ensure_card will auto-create a fresh card. Swept by size-cap in
        # cleanup_stale_cards() below.
        self._persist()
        if replaced:
            return {"status": "ok"}
        return {"status": "error", "message": "Failed to send final response"}

    async def cleanup_stale_cards(self) -> int:
        """Finalize and remove cards inactive longer than ``stale_timeout``.

        Also caps ``_pending`` and ``_origins`` at 1000 entries to prevent
        memory leaks from silent messages.
        """
        now = time.time()
        stale = [rid for rid, s in self._cards.items() if now - s.created_at > self._stale_timeout]
        for rid in stale:
            await self.finalize_card(rid, "*[Response timed out]*")

        # Cap unbounded maps
        if len(self._pending) > 1000:
            keys = list(self._pending.keys())[:500]
            for k in keys:
                self._pending.pop(k, None)
                self._origins.pop(k, None)
        if len(self._origins) > 1000:
            keys = list(self._origins.keys())[:500]
            for k in keys:
                self._origins.pop(k, None)

        self._persist()
        return len(stale)
