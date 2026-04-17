"""Reminder tools — thin shim over the legacy ``feishu_channel.reminder`` CLI.

We reuse the existing cron-wiring module so that the crontab entries written
by the live bot still work after Session 3 flips the entry point: the cron
lines call ``python -m feishu_channel.reminder send/trigger …`` which only
needs the CLI + ``_send_feishu_message`` / ``_trigger_smart_task`` helpers —
those stay in their old home.

Here we only re-export the three tool handlers (``create_reminder``,
``list_reminders``, ``delete_reminder``) and the scheduled-task watcher
coroutine that picks up smart-task files and fires them as channel
notifications.

``create_reminder`` and ``delete_reminder`` are **gated to Boss** — only the
Boss user_id may schedule or remove reminders. ``list_reminders`` is public.

TODO(session-3): deleting ``src/feishu_channel/`` will break both this
re-export AND every live crontab entry that shells out to
``python -m feishu_channel.reminder send/trigger …``. Session 3 must
either move ``reminder.py`` into ``xiaobai`` and rewrite existing cron
lines, or keep ``feishu_channel.reminder`` as a stub indefinitely.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from pathlib import Path
from typing import Awaitable, Callable

# Reuse the legacy reminder module — all cron wiring already works here.
from feishu_channel.reminder import (  # type: ignore[import-not-found]
    SCHEDULED_DIR,
    create_reminder as _create_reminder,
    delete_reminder as _delete_reminder,
    list_reminders as _list_reminders,
)

logger = logging.getLogger(__name__)

BOSS_USER_ID = "ou_8dceb221740a61fe962a5b7a5d092824"

SendNotificationFn = Callable[[str, dict], Awaitable[None]]
CreateCardFn = Callable[[str, str, str], Awaitable[None]]  # (req_id, chat_id, "")


# ── Tool handlers ────────────────────────────────────────────────


def create_reminder(
    caller_user_id: str,
    reminder_id: str,
    cron_expression: str,
    chat_id: str,
    message: str,
    smart: bool = False,
    max_runs: int = 0,
) -> dict:
    """Create a cron reminder. Boss-only."""
    if caller_user_id != BOSS_USER_ID:
        return {"status": "error", "message": "reminders are boss-only"}
    return _create_reminder(
        reminder_id, cron_expression, chat_id, message,
        smart=smart, max_runs=max_runs,
    )


def list_reminders() -> dict:
    """List all active reminders (public)."""
    return _list_reminders()


def delete_reminder(caller_user_id: str, reminder_id: str) -> dict:
    """Delete a cron reminder. Boss-only."""
    if caller_user_id != BOSS_USER_ID:
        return {"status": "error", "message": "reminders are boss-only"}
    return _delete_reminder(reminder_id)


# ── Scheduled-task watcher ───────────────────────────────────────


async def watch_scheduled_tasks(
    send_notification: SendNotificationFn,
    create_card: CreateCardFn,
    *,
    poll_seconds: float = 5.0,
) -> None:
    """Poll ``SCHEDULED_DIR`` for smart-task files and inject as notifications.

    Ported from ``feishu_channel/server.py::_watch_scheduled_tasks``.
    Broken JSON files get renamed to ``.error`` so they don't block polling.
    """
    scheduled_dir = Path(SCHEDULED_DIR)
    scheduled_dir.mkdir(parents=True, exist_ok=True)

    while True:
        await asyncio.sleep(poll_seconds)
        try:
            for task_file in sorted(scheduled_dir.glob("*.json")):
                try:
                    task = json.loads(task_file.read_text())
                    chat_id = task["chat_id"]
                    prompt = task["prompt"]
                    task_id = task.get("task_id", "unknown")

                    logger.info(
                        "Processing scheduled task %s: %s", task_id, prompt[:50]
                    )

                    request_id = str(uuid.uuid4())
                    meta = {
                        "user_id": "system_scheduler",
                        "chat_id": chat_id,
                        "sender_name": "scheduled_task",
                        "message_type": "text",
                        "request_id": request_id,
                        "message_id": "",
                        "scheduled_task_id": task_id,
                    }

                    # Pre-create a card (so reply_card has a destination)
                    try:
                        await create_card(request_id, chat_id, "")
                    except Exception as e:
                        logger.warning(
                            "Scheduled task %s: card pre-create failed: %s",
                            task_id, e,
                        )

                    content = f"[Scheduled task] {prompt}"
                    await send_notification(content, meta)

                    task_file.unlink()
                    logger.info("Scheduled task %s dispatched", task_id)
                except Exception as e:
                    logger.error(
                        "Failed to process task file %s: %s", task_file, e
                    )
                    task_file.rename(task_file.with_suffix(".error"))
        except Exception as e:
            logger.error("Scheduled task watcher error: %s", e)
