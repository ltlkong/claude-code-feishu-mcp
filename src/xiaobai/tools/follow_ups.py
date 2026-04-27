"""MCP tool: ``manage_follow_up`` — add/list/complete/cancel follow-ups.

Each ``add`` registers a smart cron reminder so the due-time fire is
handled by the existing scheduled-task watcher. When the reminder fires,
Claude receives a prompt instructing it to bring up the topic naturally
(not as a scheduled digest — the prompt is explicit about that).

Per-person notebook: when a follow-up has a ``person_id``, the manager
also re-renders ``workspace/state/todos/{person_id}.md`` so Xiaobai (and
Boss) can ``cat`` a single file to see all open + recent items for that
person — relationships/ profiles + todos/ notebooks live in parallel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..core.follow_ups import (
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    FollowUp,
    list_all,
    load,
    new_id,
    parse_due,
    save,
)
from ..reminders_cli import (
    create_reminder as _create_reminder,
    delete_reminder as _delete_reminder,
)

logger = logging.getLogger(__name__)

_TODOS_DIR = (
    Path(__file__).resolve().parents[3] / "workspace" / "state" / "todos"
)


def _cron_for(dt: datetime) -> str:
    """UTC cron expression that fires exactly once at ``dt`` (minute precision)."""
    return f"{dt.minute} {dt.hour} {dt.day} {dt.month} *"


def _sync_person_todos(person_id: str) -> None:
    """Re-render ``workspace/state/todos/{person_id}.md`` from all follow-ups for that person.

    Idempotent. Pending items first (sorted by due_at), then completed/cancelled
    history below for context. No-op when ``person_id`` is empty.
    """
    if not person_id:
        return
    try:
        _TODOS_DIR.mkdir(parents=True, exist_ok=True)
        pending = list_all(person_id=person_id, status=STATUS_PENDING)
        done = list_all(person_id=person_id, status=STATUS_COMPLETED)
        cancelled = list_all(person_id=person_id, status=STATUS_CANCELLED)

        lines = [f"# Todos — {person_id}", ""]

        if pending:
            lines.append("## Pending")
            lines.append("")
            for f in pending:
                lines.append(f"### {f.due_at} — {f.topic}")
                lines.append(f"- id: `{f.id}`")
                lines.append(f"- chat: `{f.chat_id}`")
                if f.context:
                    lines.append(f"- context: {f.context}")
                lines.append("")

        if done:
            lines.append("## Completed (recent)")
            lines.append("")
            for f in sorted(done, key=lambda x: x.due_at or "", reverse=True)[:20]:
                lines.append(f"- ✓ {f.due_at} — {f.topic}" + (f" — {f.note}" if f.note else ""))
            lines.append("")

        if cancelled:
            lines.append("## Cancelled (recent)")
            lines.append("")
            for f in sorted(cancelled, key=lambda x: x.due_at or "", reverse=True)[:10]:
                lines.append(f"- ✗ {f.due_at} — {f.topic}" + (f" — {f.note}" if f.note else ""))
            lines.append("")

        path = _TODOS_DIR / f"{person_id}.md"
        path.write_text("\n".join(lines), encoding="utf-8")
    except Exception as e:
        logger.warning("todos notebook sync failed for %s: %s", person_id, e)


def _smart_prompt(topic: str, context: str) -> str:
    """Prompt injected when the reminder fires. Keeps the bot from sounding like a robot."""
    parts = [
        "Follow up on a topic you committed to earlier.",
        f"Topic: {topic}",
    ]
    if context:
        parts.append(f"Context you saved at the time: {context}")
    parts.append(
        "Send a single natural question or check-in — no 'reminder from the system' "
        "framing, no mention of follow-ups or cron. Match the existing tone of the chat."
    )
    return "\n".join(parts)


def manage_follow_up(
    action: str,
    *,
    follow_up_id: str = "",
    chat_id: str = "",
    person_id: str = "",
    topic: str = "",
    context: str = "",
    due_at: str = "",
    note: str = "",
) -> dict:
    """Add / list / complete / cancel a conversational follow-up.

    Actions:
      - ``add``: required chat_id + topic + due_at. Persists the follow-up
        and registers a one-shot smart reminder.
      - ``list``: optional chat_id / person_id filter; optional
        ``due_within_hours`` later via call-site kwargs if needed. Returns
        pending follow-ups sorted by due time.
      - ``complete``: marks the follow-up done; stores ``note`` if given.
        Deletes the backing reminder.
      - ``cancel``: marks cancelled + deletes the reminder.
    """
    if action == "add":
        return _handle_add(chat_id, person_id, topic, context, due_at)
    if action == "list":
        return _handle_list(chat_id=chat_id, person_id=person_id)
    if action in ("complete", "cancel"):
        return _handle_close(follow_up_id, action, note)
    return {"status": "error", "message": f"unknown action: {action}"}


# ── Action handlers ───────────────────────────────────────────────


def _handle_add(
    chat_id: str, person_id: str, topic: str, context: str, due_at: str
) -> dict:
    if not chat_id:
        return {"status": "error", "message": "chat_id is required"}
    if not topic:
        return {"status": "error", "message": "topic is required"}
    if not due_at:
        return {"status": "error", "message": "due_at is required (ISO, +3d, etc)"}

    due_dt = parse_due(due_at)
    if due_dt is None:
        return {"status": "error", "message": f"could not parse due_at: {due_at!r}"}
    if due_dt <= datetime.now(timezone.utc):
        return {"status": "error", "message": "due_at must be in the future"}

    fu_id = new_id()
    reminder_id = f"followup_{fu_id}"
    cron_expr = _cron_for(due_dt)

    fu = FollowUp(
        id=fu_id,
        chat_id=chat_id,
        person_id=person_id,
        topic=topic[:140],
        context=context[:1000],
        due_at=due_dt.isoformat(timespec="minutes").replace("+00:00", "Z"),
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        status=STATUS_PENDING,
        reminder_id=reminder_id,
    )

    # Register the backing smart reminder. Fire once (max_runs=1).
    reminder_result = _create_reminder(
        reminder_id=reminder_id,
        cron_expr=cron_expr,
        chat_id=chat_id,
        message=_smart_prompt(topic, context),
        smart=True,
        max_runs=1,
    )
    if reminder_result.get("status") != "ok":
        return {
            "status": "error",
            "message": f"reminder registration failed: {reminder_result.get('message')}",
        }

    save(fu)
    _sync_person_todos(person_id)
    return {
        "status": "ok",
        "follow_up_id": fu.id,
        "due_at": fu.due_at,
        "reminder_id": reminder_id,
    }


def _handle_list(*, chat_id: str, person_id: str) -> dict:
    items = list_all(chat_id=chat_id, person_id=person_id, status=STATUS_PENDING)
    return {
        "status": "ok",
        "count": len(items),
        "items": [
            {
                "id": f.id,
                "chat_id": f.chat_id,
                "person_id": f.person_id,
                "topic": f.topic,
                "context": f.context,
                "due_at": f.due_at,
                "status": f.status,
            }
            for f in items
        ],
    }


def _handle_close(follow_up_id: str, action: str, note: str) -> dict:
    if not follow_up_id:
        return {"status": "error", "message": "follow_up_id is required"}
    fu = load(follow_up_id)
    if fu is None:
        return {"status": "error", "message": f"no such follow-up: {follow_up_id}"}

    new_status = STATUS_COMPLETED if action == "complete" else STATUS_CANCELLED
    fu.status = new_status
    if note:
        fu.note = note[:500]
    save(fu)

    # Best-effort reminder cleanup — if it already fired or was deleted, fine.
    if fu.reminder_id:
        try:
            _delete_reminder(fu.reminder_id)
        except Exception as e:
            logger.debug("follow-up %s reminder cleanup skipped: %s", follow_up_id, e)

    _sync_person_todos(fu.person_id)
    return {"status": "ok", "follow_up_id": fu.id, "new_status": new_status}
