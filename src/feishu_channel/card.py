# src/feishu_channel/card.py
"""Card manager for two-section Feishu cards (action + response text).

Card layout:
┌──────────────────────────────┐
│ {action_text}                │  <- action section (updates in place)
├──────────────────────────────┤
│ {response_text}              │  <- response section (grows over time)
└──────────────────────────────┘
"""

import json
import time
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

def _build_card_json(action: str, text: str, streaming: bool = True,
                     emoji: str = "", template: str = "indigo") -> str:
    """Build a v2 (CardKit) card JSON with header and content.

    When action is provided (streaming state), shown as card header with emoji.
    When action is empty (finalized state), no header.
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

    card = {
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
    """Flatten V1 'action' wrapper tags into direct elements for V2 compatibility.

    V1: {"tag": "action", "actions": [{"tag": "button", ...}, ...]}
    V2: {"tag": "button", ...}, {"tag": "button", ...}
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
    message_id: str | None = None  # Feishu message ID (set after card is created)
    card_id: str | None = None     # CardKit card ID
    sequence: int = 0
    created_at: float = field(default_factory=time.time)


class CardManager:
    """Creates and updates two-section Feishu cards.

    Uses CardKit API (streaming mode) as primary, PATCH as fallback.
    """

    def __init__(self, app_id: str, app_secret: str, stale_timeout_minutes: int = 30):
        self._http = httpx.AsyncClient(timeout=10)
        self._app_id = app_id
        self._app_secret = app_secret
        self._stale_timeout = stale_timeout_minutes * 60
        self._token: str | None = None
        self._token_time: float = 0
        # request_id -> CardState
        self._cards: dict[str, CardState] = {}

    async def _get_token(self) -> str:
        """Get cached tenant token (refresh every 90 min)."""
        if self._token and (time.time() - self._token_time) < 5400:
            return self._token
        from .media import get_tenant_token
        self._token = await get_tenant_token(self._http, self._app_id, self._app_secret)
        self._token_time = time.time()
        logger.info("Refreshed tenant token")
        return self._token

    def _invalidate_token(self) -> None:
        """Force token refresh on next request."""
        self._token = None
        self._token_time = 0

    async def _headers(self) -> dict[str, str]:
        token = await self._get_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # ── CardKit API ──────────────────────────────────────────────

    def _is_token_error(self, data: dict) -> bool:
        """Check if API response indicates an expired/invalid token."""
        return data.get("code") in (99991663, 99991664, 99991668)

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
                    self._invalidate_token()
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
                logger.debug("CardKit replace: card_id=%s seq=%d body_size=%d", card_id, seq, len(json.dumps(body)))
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
                    self._invalidate_token()
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
                    json={"settings": json.dumps({"streaming_mode": False}), "sequence": seq},
                )
                data = resp.json()
                if attempt == 0 and self._is_token_error(data):
                    logger.info("CardKit finalize: token expired, refreshing and retrying")
                    self._invalidate_token()
                    continue
                return
            except Exception as e:
                logger.warning("CardKit finalize error: %s", e)
            break

    # ── Feishu IM API ────────────────────────────────────────────

    async def _send_card_message(self, chat_id: str, reply_to: str, card_json: str, card_id: str | None) -> str | None:
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
                    f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers=headers,
                    json=body,
                )
                data = resp.json()
                if data.get("code") == 0:
                    return data["data"]["message_id"]
                if attempt == 0 and self._is_token_error(data):
                    logger.info("Send card: token expired, refreshing and retrying")
                    self._invalidate_token()
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
                    self._invalidate_token()
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
                    self._invalidate_token()
                    continue
                logger.warning("Patch card failed: %s", data)
            except Exception as e:
                logger.warning("Patch card error: %s", e)
            break
        return False

    # ── File upload & send ───────────────────────────────────────

    async def _upload_and_send(self, chat_id: str, file_path: str, file_type: str, msg_type: str, mime: str | None = None) -> dict:
        """Upload a file to Feishu and send it as a message.

        Args:
            file_type: Feishu upload type ("stream" for files, "opus" for audio)
            msg_type: Feishu message type ("file" or "audio")
            mime: Optional MIME type override for the upload
        """
        from pathlib import Path
        path = Path(file_path)
        if not path.exists():
            return {"status": "error", "message": f"File not found: {file_path}"}

        for attempt in range(2):
            try:
                token = await self._get_token()
                file_tuple = (path.name, open(path, "rb"), mime) if mime else (path.name, open(path, "rb"))
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
                    self._invalidate_token()
                    continue
                if data.get("code") != 0:
                    return {"status": "error", "message": f"Upload failed: {data}"}
                file_key = data["data"]["file_key"]

                headers = await self._headers()
                resp = await self._http.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
                    headers=headers,
                    json={"receive_id": chat_id, "msg_type": msg_type, "content": json.dumps({"file_key": file_key})},
                )
                data = resp.json()
                if data.get("code") == 0:
                    return {"status": "ok"}
                if attempt == 0 and self._is_token_error(data):
                    logger.debug("Send %s: token expired, retrying", msg_type)
                    self._invalidate_token()
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
        """Adopt an existing card so update_card/finalize_card can update it in place."""
        self._cards[request_id] = CardState(
            chat_id=chat_id,
            reply_to_message_id="",
            message_id=message_id,
        )
        logger.info("adopt_card: request_id=%s message_id=%s", request_id, message_id)

    async def create_card(self, request_id: str, chat_id: str, reply_to_message_id: str) -> None:
        """Create a new card in 'thinking...' state. Called when a notification is emitted."""
        card_json = _build_card_json("", "💭...", streaming=True)
        card_id = await self._cardkit_create(card_json)
        message_id = await self._send_card_message(chat_id, reply_to_message_id, card_json, card_id)

        self._cards[request_id] = CardState(
            chat_id=chat_id,
            reply_to_message_id=reply_to_message_id,
            message_id=message_id,
            card_id=card_id,
        )

    async def update_card(self, request_id: str, status: str, text: str,
                          emoji: str = "", template: str = "indigo") -> dict:
        """Update a card's status header and response text."""
        state = self._cards.get(request_id)
        if not state:
            logger.warning("update_card: no card for request_id %s", request_id)
            return {"status": "error", "message": f"No card for request_id {request_id}"}

        state.sequence += 1
        card_json = _build_card_json(status, text, streaming=True, emoji=emoji, template=template)
        logger.debug("update_card: request_id=%s card_id=%s message_id=%s seq=%d",
                     request_id, state.card_id, state.message_id, state.sequence)

        if state.card_id:
            if await self._cardkit_replace(state.card_id, card_json, state.sequence):
                return {"status": "ok"}
            logger.warning("update_card: CardKit replace failed, trying PATCH fallback")

        if state.message_id:
            if await self._patch_card(state.message_id, card_json):
                return {"status": "ok"}

        return {"status": "error", "message": "Failed to update card"}

    def _is_card_json(self, text: str) -> bool:
        """Check if text is a Feishu V2 card JSON."""
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
        state = self._cards.get(request_id)
        if not state:
            return {"status": "error", "message": f"No card for request_id {request_id}"}

        state.sequence += 1

        # If Claude sent a V2 card JSON, use it directly. Otherwise wrap in content card.
        is_card = self._is_card_json(text)
        if is_card:
            card_dict = json.loads(text.strip())
            card_dict.pop("config", None)  # Let CardKit manage config
            # Flatten V1 "action" wrappers — V2 doesn't support them
            if "body" in card_dict and "elements" in card_dict["body"]:
                card_dict["body"]["elements"] = _flatten_action_tags(card_dict["body"]["elements"])
            card_json = json.dumps(card_dict, ensure_ascii=False)
        else:
            card_json = _build_card_json("", text, streaming=False)

        logger.debug("finalize_card: request_id=%s is_card=%s card_id=%s message_id=%s seq=%d card_json_len=%d",
                     request_id, is_card, state.card_id, state.message_id, state.sequence, len(card_json))

        # CardKit replace (primary) — card was created via CardKit
        replaced = False
        if state.card_id:
            replaced = await self._cardkit_replace(state.card_id, card_json, state.sequence)
            if replaced:
                state.sequence += 1
                await self._cardkit_finalize(state.card_id, state.sequence)
            else:
                logger.warning("finalize_card: CardKit replace failed for card_id=%s, trying PATCH", state.card_id)

        if not replaced and state.message_id:
            replaced = await self._patch_card(state.message_id, card_json)
            logger.debug("finalize_card: PATCH fallback result=%s for message_id=%s", replaced, state.message_id)

        if not replaced and not state.card_id and not state.message_id:
            # No card was ever created (e.g. token was expired at creation time).
            # Try to create and send a fresh card now.
            logger.debug("finalize_card: no card exists, creating fresh card for request_id=%s", request_id)
            card_id = await self._cardkit_create(card_json)
            message_id = await self._send_card_message(state.chat_id, state.reply_to_message_id, card_json, card_id)
            replaced = message_id is not None

        del self._cards[request_id]
        if replaced:
            return {"status": "ok"}
        return {"status": "error", "message": "Failed to send final response"}

    async def cleanup_stale_cards(self) -> int:
        """Finalize and remove cards that haven't been updated in stale_timeout."""
        now = time.time()
        stale = [rid for rid, s in self._cards.items() if now - s.created_at > self._stale_timeout]
        for rid in stale:
            await self.finalize_card(rid, "*[Response timed out]*")
        return len(stale)
