"""Profile storage + alias maps (chat + user).

Ported from ``feishu_channel/server.py::_handle_update_profile`` /
``_load_profile`` (profile I/O) and ``feishu_channel/heartbeat.py``
(alias maps — the alias code was misfiled in heartbeat.py and moves here
per the Session 2 design).

The on-disk format is preserved byte-for-byte: one Markdown file per
``{chat_id}_{user_id}`` in the project-root ``profiles/`` directory.

Chat aliases live in the heartbeat watchlist JSON (read-only from this
module — writes happen via ``tools.heartbeat.manage_heartbeat``).
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)


# ── Paths ────────────────────────────────────────────────────────

# Project root = 3 levels above this file (src/xiaobai/tools/profile.py)
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PROFILES_DIR = _PROJECT_ROOT / "profiles"
_WATCHLIST_FILE = _PROJECT_ROOT / "workspace" / "state" / "heartbeat_watchlist.json"

_PROFILE_MAX_CHARS = 500


# ── User ID aliases ──────────────────────────────────────────────

_user_aliases: dict[str, str] = {}  # user_id -> short name


def _extract_name_from_profile(content: str) -> str:
    """Extract short name from ``**Name (Title)** — …`` header."""
    m = re.match(r"\*\*(.+?)\*\*", content)
    if m:
        name = m.group(1)
        # Drop the "(Title)" part for shortest form
        name = name.split("(")[0].strip()
        return name
    return ""


def register_user_alias(user_id: str, profile: str) -> None:
    """Record ``user_id -> name`` from a profile body. Deduplicates names."""
    if not user_id or not profile:
        return
    name = _extract_name_from_profile(profile)
    if not name:
        return
    # Ensure unique — append last 2 chars of user_id if conflict
    for uid, existing_name in _user_aliases.items():
        if uid != user_id and existing_name == name:
            name = f"{name}_{user_id[-2:]}"
            break
    _user_aliases[user_id] = name


def get_user_alias(user_id: str) -> str:
    """Return the short alias for a user_id, or its 12-char prefix."""
    return _user_aliases.get(user_id, user_id[:12])


def resolve_user_alias(alias_or_id: str) -> str:
    """Resolve a name back to an ``ou_…`` user_id, or pass through unchanged."""
    if alias_or_id.startswith("ou_"):  # already a full ID
        return alias_or_id
    for uid, name in _user_aliases.items():
        if name == alias_or_id:
            return uid
    return alias_or_id


# ── Chat aliases (read-only — watchlist is owned by heartbeat.py) ──

def _load_watchlist() -> dict:
    if _WATCHLIST_FILE.is_file():
        try:
            return json.loads(_WATCHLIST_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def resolve_alias(alias_or_id: str) -> str:
    """Resolve a chat label to its full ``oc_…`` / ``@im.wechat`` chat_id."""
    if len(alias_or_id) > 20:  # already a full ID
        return alias_or_id
    wl = _load_watchlist()
    for chat_id, info in wl.items():
        if info.get("label", "") == alias_or_id:
            return chat_id
    return alias_or_id


def get_alias(chat_id: str) -> str:
    """Return the chat's friendly label, or its 12-char prefix."""
    wl = _load_watchlist()
    if chat_id in wl:
        return wl[chat_id].get("label", chat_id[:12])
    return chat_id[:12]


# ── Profile I/O ──────────────────────────────────────────────────

def _profile_path(chat_id: str, user_id: str) -> Path:
    safe_chat = re.sub(r"[^a-zA-Z0-9_\-]", "_", chat_id)
    safe_user = re.sub(r"[^a-zA-Z0-9_\-]", "_", user_id)
    return _PROFILES_DIR / f"{safe_chat}_{safe_user}.md"


def load_profile(chat_id: str, user_id: str) -> str:
    """Read profile body for ``(chat_id, user_id)``, or empty string."""
    path = _profile_path(chat_id, user_id)
    if path.is_file():
        try:
            return path.read_text().strip()
        except OSError:
            return ""
    return ""


# ── Tool handlers ────────────────────────────────────────────────


def update_profile(
    chat_id: str,
    user_id: str,
    *,
    name: str,
    title: str = "",
    real_name: str = "",
    location: str = "",
    phone: str = "",
    notes: str = "",
) -> dict:
    """Build the standard template and persist to ``profiles/``.

    Template (matches old behavior):

        **Name (Title)** — Real name, Location, Phone
        Free-form notes…
    """
    header = f"**{name}"
    if title:
        header += f" ({title})"
    header += "**"
    details = ", ".join(p for p in [real_name, location, phone] if p)
    if details:
        header += f" — {details}"

    profile = header
    if notes:
        profile += f"\n{notes}"

    if len(profile) > _PROFILE_MAX_CHARS:
        return {
            "status": "error",
            "message": (
                f"Profile too long ({len(profile)}/{_PROFILE_MAX_CHARS}). "
                "Shorten notes."
            ),
        }

    os.makedirs(_PROFILES_DIR, exist_ok=True)
    path = _profile_path(chat_id, user_id)
    path.write_text(profile)

    # Register alias immediately so the in-memory map reflects the new profile
    register_user_alias(user_id, profile)

    return {"status": "ok", "file": path.name, "profile": profile}


async def get_user_info(feishu, user_id: str, download_avatar: bool = False) -> dict:
    """Call Feishu contact API. ``feishu`` is a ``FeishuChannel`` instance."""
    token_provider = feishu.token
    http = feishu.http
    from ..channels.feishu.api import is_token_error

    for attempt in range(2):
        try:
            token = await token_provider.get()
            headers = {"Authorization": f"Bearer {token}"}
            resp = await http.get(
                f"https://open.feishu.cn/open-apis/contact/v3/users/{user_id}",
                headers=headers,
                params={"user_id_type": "open_id"},
            )
            data = resp.json()
            if attempt == 0 and is_token_error(data):
                token_provider.invalidate()
                continue
            if data.get("code") != 0:
                return {
                    "status": "error",
                    "message": f"User info failed: {data.get('message', '')}",
                }
            user = data["data"]["user"]
            avatar = user.get("avatar", {})
            result = {
                "status": "ok",
                "name": user.get("name", ""),
                "en_name": user.get("en_name", ""),
                "avatar_url": avatar.get("avatar_origin") or avatar.get("avatar_240", ""),
                "mobile": user.get("mobile", ""),
                "email": user.get("email", ""),
                "department_ids": user.get("department_ids", []),
            }
            if download_avatar and result["avatar_url"]:
                img_resp = await http.get(result["avatar_url"])
                ext = (
                    "jpg"
                    if "jpg" in result["avatar_url"] or "jpeg" in result["avatar_url"]
                    else "png"
                )
                # feishu.settings isn't exposed; use temp_dir indirectly
                out_path = Path(f"/tmp/feishu-channel/avatar_{user_id}.{ext}")
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_bytes(img_resp.content)
                result["avatar_local_path"] = str(out_path)
            return result
        except Exception as e:
            return {"status": "error", "message": f"Get user info error: {e}"}
    return {"status": "error", "message": "Get user info failed after retry"}
