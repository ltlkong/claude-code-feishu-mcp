"""Heartbeat watchlist + proactive-messaging loop.

Ported from ``feishu_channel/heartbeat.py`` minus the alias maps that
moved to :mod:`.profile`. The watchlist on-disk format
(``workspace/state/heartbeat_watchlist.json``) is preserved.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Awaitable, Callable

from .profile import get_alias

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_WATCHLIST_FILE = _PROJECT_ROOT / "workspace" / "state" / "heartbeat_watchlist.json"
# Permanent mute — an explicit ``remove`` action adds the chat here so
# the message-count auto-add logic can't put it back silently.
_MUTE_FILE = _PROJECT_ROOT / "workspace" / "state" / "heartbeat_mute.json"

_inactivity_threshold_minutes: int = 10
_DEFAULT_INTERVAL: int = 15  # minutes
_POLL_SECONDS: int = 60  # inner loop tick

# Adaptive-interval constants.
# Each time a heartbeat fires and neither the user nor the bot says
# anything before the next tick, silent_ticks++. The effective interval
# grows by SILENT_GROWTH_MINUTES per silent tick, capped at MAX_INTERVAL.
# User/bot activity resets silent_ticks to 0 so the cadence snaps back
# as soon as the chat wakes up.
SILENT_GROWTH_MINUTES: int = 10
MAX_INTERVAL_MINUTES: int = 240  # 4 hours


def _effective_interval(info: dict) -> int:
    """Return the adaptive interval (in minutes) for a watchlist entry."""
    base = info.get("interval", _DEFAULT_INTERVAL)
    silent = info.get("silent_ticks", 0)
    return min(MAX_INTERVAL_MINUTES, base + silent * SILENT_GROWTH_MINUTES)


def configure_inactivity(minutes: int) -> None:
    """Set the inactivity threshold (minutes) for proactive nudges."""
    global _inactivity_threshold_minutes
    _inactivity_threshold_minutes = minutes


# ── Watchlist I/O ────────────────────────────────────────────────

def _load_watchlist() -> dict[str, dict]:
    if _WATCHLIST_FILE.is_file():
        try:
            return json.loads(_WATCHLIST_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            return {}
    return {}


def _save_watchlist(data: dict) -> None:
    _WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    _WATCHLIST_FILE.write_text(json.dumps(data, indent=2))


# ── Permanent-mute list ──────────────────────────────────────────

def _load_mute() -> set[str]:
    if _MUTE_FILE.is_file():
        try:
            raw = json.loads(_MUTE_FILE.read_text())
            if isinstance(raw, list):
                return {str(x) for x in raw}
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    return set()


def _save_mute(ids: set[str]) -> None:
    _MUTE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _MUTE_FILE.write_text(json.dumps(sorted(ids), indent=2))


def _mute(chat_id: str) -> None:
    if not chat_id:
        return
    mute = _load_mute()
    if chat_id not in mute:
        mute.add(chat_id)
        _save_mute(mute)


def _unmute(chat_id: str) -> None:
    if not chat_id:
        return
    mute = _load_mute()
    if chat_id in mute:
        mute.remove(chat_id)
        _save_mute(mute)


# ── Tool handler: manage_heartbeat ───────────────────────────────

def manage_heartbeat(
    action: str, chat_id: str = "", label: str = "", interval: int = 0
) -> dict:
    """Add / remove / list / set_interval on the watchlist."""
    wl = _load_watchlist()

    if action == "list":
        items = [{"chat_id": cid, **info} for cid, info in wl.items()]
        return {
            "status": "ok",
            "count": len(items),
            "default_interval": _DEFAULT_INTERVAL,
            "chats": items,
        }

    if action == "add":
        if not chat_id:
            return {"status": "error", "message": "chat_id required"}
        iv = interval if interval >= 10 else _DEFAULT_INTERVAL
        final_label = label or chat_id[:12]
        existing_labels = {
            info.get("label") for cid, info in wl.items() if cid != chat_id
        }
        if final_label in existing_labels:
            final_label = f"{final_label}_{chat_id[-2:]}"
        wl[chat_id] = {
            "added": time.time(),
            "label": final_label,
            "interval": iv,
            "last_checked": 0,
        }
        _save_watchlist(wl)
        # Explicit add → clear any prior mute so the chat is actually active.
        _unmute(chat_id)
        return {"status": "ok", "message": f"Added {chat_id[:12]} ({iv}m)"}

    if action == "remove":
        if not chat_id:
            return {"status": "error", "message": "chat_id required"}
        # Mute permanently so auto-add can't silently put it back on the
        # next message burst. Explicit future `add` clears the mute.
        _mute(chat_id)
        if chat_id in wl:
            del wl[chat_id]
            _save_watchlist(wl)
            return {"status": "ok", "message": f"Removed + muted {chat_id[:12]}"}
        return {"status": "ok", "message": f"Muted {chat_id[:12]}"}

    if action == "set_interval":
        if not chat_id:
            return {"status": "error", "message": "chat_id required"}
        if interval < 10:
            return {"status": "error", "message": "interval must be >= 10 minutes"}
        if chat_id not in wl:
            return {"status": "error", "message": "Chat not in watchlist"}
        wl[chat_id]["interval"] = interval
        _save_watchlist(wl)
        return {
            "status": "ok",
            "message": f"{chat_id[:12]} interval set to {interval}m",
        }

    return {"status": "error", "message": f"Unknown action: {action}"}


# ── Activity tracking ────────────────────────────────────────────

_last_activity: dict[str, float] = {}           # last INBOUND message per chat
_last_bot_reply: dict[str, float] = {}          # last OUTBOUND message from bot
_msg_counts: dict[str, int] = {}                # lifetime message count per chat
_pending_auto_labels: dict[str, str] = {}

# Auto-add / auto-cleanup thresholds
_AUTO_ADD_MSG_THRESHOLD = 5          # require N messages before auto-adding
_AUTO_CLEANUP_IDLE_DAYS = 7          # drop auto-added chats idle this long


def mark_activity(chat_id: str = "", label: str = "") -> None:
    """Record inbound user activity.

    Auto-adds a chat after >= N messages. Silent-tick backoff in the
    main loop reads ``_last_activity`` to decide whether to reset the
    per-chat ``silent_ticks`` counter — so simply recording a fresh
    timestamp is enough; no watchlist write on the hot path.
    """
    if not chat_id:
        return
    _last_activity[chat_id] = time.time()
    _msg_counts[chat_id] = _msg_counts.get(chat_id, 0) + 1
    if label:
        _pending_auto_labels[chat_id] = label


def mark_bot_reply(chat_id: str = "") -> None:
    """Record an outbound bot reply to ``chat_id``.

    Used by heartbeat backoff to tell "silence because nobody spoke"
    from "silence because I already said my piece." Both paths reset
    the silent-tick counter on the next loop tick.
    """
    if not chat_id:
        return
    _last_bot_reply[chat_id] = time.time()


def _flush_auto_adds() -> None:
    """Persist auto-adds for chats that have crossed the message threshold.

    Chats in the permanent mute list are skipped — a prior ``remove``
    cannot be undone by the auto-add heuristic.
    """
    mute = _load_mute()
    wl = _load_watchlist()
    changed = False
    for chat_id, count in list(_msg_counts.items()):
        if chat_id in wl:
            continue
        if count < _AUTO_ADD_MSG_THRESHOLD:
            continue
        if chat_id in mute:
            continue
        lbl = _pending_auto_labels.get(chat_id, "") or chat_id[:12]
        wl[chat_id] = {
            "added": time.time(),
            "label": lbl,
            "auto": True,
            "interval": _DEFAULT_INTERVAL,
            "last_checked": 0,
        }
        changed = True
        logger.info("Heartbeat: auto-added %s (%d msgs) to watchlist", chat_id[:12], count)
    if changed:
        _save_watchlist(wl)


def _cleanup_idle_autos() -> None:
    """Remove auto-added chats with no activity for _AUTO_CLEANUP_IDLE_DAYS."""
    wl = _load_watchlist()
    cutoff = time.time() - _AUTO_CLEANUP_IDLE_DAYS * 86400
    removed = []
    for chat_id, info in list(wl.items()):
        if not info.get("auto"):
            continue
        last = _last_activity.get(chat_id, info.get("added", 0))
        if last < cutoff:
            del wl[chat_id]
            removed.append(chat_id[:12])
    if removed:
        _save_watchlist(wl)
        logger.info("Heartbeat: auto-cleanup removed %d idle chats: %s", len(removed), removed)


def is_chat_inactive(chat_id: str) -> bool:
    last = _last_activity.get(chat_id, 0)
    return (time.time() - last) >= _inactivity_threshold_minutes * 60


# ── Main loop ────────────────────────────────────────────────────

NotifyFn = Callable[[str, dict], Awaitable[None]]
QuietCheckFn = Callable[[str], bool]


async def heartbeat_loop(
    interval_minutes: int = 15,
    notify_fn: NotifyFn | None = None,
    quiet_check: QuietCheckFn | None = None,
    **_kwargs,
) -> None:
    """Periodically nudge Claude to check on inactive watched chats.

    Polls every 60s. Each chat has its own ``interval`` — only chats whose
    interval has elapsed AND that are inactive get a notification.
    """
    global _DEFAULT_INTERVAL
    _DEFAULT_INTERVAL = interval_minutes
    logger.info(
        "Heartbeat started: watchlist mode, default_interval=%dm, inactivity=%dm",
        _DEFAULT_INTERVAL,
        _inactivity_threshold_minutes,
    )

    await asyncio.sleep(60)

    while True:
        try:
            if not notify_fn:
                await asyncio.sleep(_POLL_SECONDS)
                continue

            _flush_auto_adds()
            _cleanup_idle_autos()
            watchlist = _load_watchlist()
            if not watchlist:
                await asyncio.sleep(_POLL_SECONDS)
                continue

            now = time.time()
            inactive_labels: list[str] = []
            inactive_ids: list[str] = []
            save_needed = False

            for chat_id, info in watchlist.items():
                last_checked = info.get("last_checked", 0)
                # Reset silent_ticks whenever either party spoke since the
                # last heartbeat fire. This is the "snap back on activity"
                # half of the adaptive-interval design — no matter how long
                # a dead chat's interval has grown, one real message brings
                # it back to base.
                user_activity = _last_activity.get(chat_id, 0)
                bot_activity = _last_bot_reply.get(chat_id, 0)
                if max(user_activity, bot_activity) > last_checked:
                    if info.get("silent_ticks", 0) > 0:
                        info["silent_ticks"] = 0
                        save_needed = True
                # Adaptive interval: base_interval + silent_ticks * growth.
                chat_interval = _effective_interval(info) * 60

                if now - last_checked < chat_interval:
                    continue
                if not is_chat_inactive(chat_id):
                    continue

                # Server-side quiet hours — don't wake the user during their
                # deep_night / late_night window. Skipping here means we also
                # don't advance last_checked or silent_ticks, so the next
                # eligible tick after their local morning will fire cleanly.
                if quiet_check is not None:
                    try:
                        if quiet_check(chat_id):
                            continue
                    except Exception as e:
                        logger.debug("quiet_check error for %s: %s", chat_id, e)

                label = info.get("label", chat_id[:12])
                inactive_labels.append(f"{label}({chat_id[:12]})")
                inactive_ids.append(chat_id)
                watchlist[chat_id]["last_checked"] = now
                # Tentatively tick silent_ticks up; mark_activity /
                # mark_bot_reply will reset it back to 0 when someone speaks.
                watchlist[chat_id]["silent_ticks"] = info.get("silent_ticks", 0) + 1
                save_needed = True

            if save_needed:
                _save_watchlist(watchlist)

            for i, chat_id in enumerate(inactive_ids):
                label = inactive_labels[i]
                alias = get_alias(chat_id)
                content = (
                    f"Heartbeat: check on {alias}\n"
                    "Follow HEARTBEAT.md checklist before deciding."
                )
                meta = {
                    "source": "heartbeat",
                    "chat_id": alias,
                    "suggestion": f"Check on {alias}",
                }
                await notify_fn(content, meta)
                logger.info("Heartbeat: nudged for %s", label)

        except Exception as e:
            logger.error("Heartbeat loop error: %s", e)

        await asyncio.sleep(_POLL_SECONDS)
