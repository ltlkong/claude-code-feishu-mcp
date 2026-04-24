"""Follow-up tracker — the "我昨天说的那事怎样了" memory.

When someone mentions a time-anchored event ("下周体检", "周末见某人",
"这个月底交稿"), a real friend remembers and brings it back up naturally
later. The follow-up store is the mechanism that makes this possible
without turning into a scheduled-digest bot.

Each follow-up is a single JSON file under ``workspace/state/follow_ups/``
with a short topic, free-form context, a due time, and a status. A smart
reminder is registered at the due time via ``reminders_cli.create_reminder``;
when it fires, the existing scheduled-task watcher re-enters Claude with
a prompt instructing it to bring up the topic naturally (not mechanically).

MCP handler: ``tools/follow_ups.manage_follow_up``. Actions: add, list,
complete, cancel.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


_STORE_DIR = (
    Path(__file__).resolve().parents[2]
    / ".." / "workspace" / "state" / "follow_ups"
).resolve()


STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_CANCELLED = "cancelled"


@dataclass
class FollowUp:
    id: str
    chat_id: str
    person_id: str
    topic: str              # short: "体检结果" / "莱阳旅居项目进展"
    context: str             # free text: the user's own words, relevant details
    due_at: str              # ISO UTC "2026-04-24T10:00:00Z"
    created_at: str          # ISO UTC
    status: str = STATUS_PENDING
    reminder_id: str = ""    # linked smart reminder (for cancel)
    note: str = ""           # optional — what happened when we checked in

    @classmethod
    def from_dict(cls, data: dict) -> "FollowUp":
        return cls(
            id=str(data["id"]),
            chat_id=str(data.get("chat_id", "")),
            person_id=str(data.get("person_id", "")),
            topic=str(data.get("topic", "")),
            context=str(data.get("context", "")),
            due_at=str(data.get("due_at", "")),
            created_at=str(data.get("created_at", "")),
            status=str(data.get("status", STATUS_PENDING)),
            reminder_id=str(data.get("reminder_id", "")),
            note=str(data.get("note", "")),
        )


# ── Natural due-time parsing ──────────────────────────────────────

_RELATIVE_RE = re.compile(r"^\s*\+?(\d+)\s*([hdw])\s*$", re.I)


def parse_due(due: str, *, now: datetime | None = None) -> datetime | None:
    """Parse a due string into a UTC datetime.

    Supports:
    - ISO 8601 (``2026-05-01T10:00:00Z``, ``2026-05-01 10:00:00+08:00``)
    - Relative: ``+3d``, ``+72h``, ``+2w``, ``1d`` (no sign)
    - Bare date: ``2026-05-01`` (interpreted as UTC midnight)
    """
    if not due:
        return None
    s = due.strip()
    ref = now or datetime.now(timezone.utc)

    # Relative (+3d, 72h, 2w).
    m = _RELATIVE_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
        return ref + delta

    # Bare date.
    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        return datetime.fromisoformat(f"{s}T00:00:00+00:00")

    # Full ISO.
    try:
        normalized = s.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


# ── Storage I/O ────────────────────────────────────────────────────


def _path_for(follow_up_id: str, root: Path | None = None) -> Path:
    base = root or _STORE_DIR
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", follow_up_id)
    return base / f"{safe}.json"


def save(follow_up: FollowUp, *, root: Path | None = None) -> Path:
    base = root or _STORE_DIR
    base.mkdir(parents=True, exist_ok=True)
    p = _path_for(follow_up.id, root=base)
    p.write_text(json.dumps(asdict(follow_up), ensure_ascii=False, indent=2))
    return p


def load(follow_up_id: str, *, root: Path | None = None) -> FollowUp | None:
    p = _path_for(follow_up_id, root=root)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
        return FollowUp.from_dict(data)
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as e:
        logger.warning("follow-up %s parse failed: %s", follow_up_id, e)
        return None


def list_all(
    *,
    chat_id: str = "",
    person_id: str = "",
    status: str = STATUS_PENDING,
    due_within_hours: int | None = None,
    root: Path | None = None,
) -> list[FollowUp]:
    base = root or _STORE_DIR
    if not base.is_dir():
        return []
    out: list[FollowUp] = []
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=due_within_hours) if due_within_hours else None
    for p in sorted(base.glob("*.json")):
        try:
            fu = FollowUp.from_dict(json.loads(p.read_text()))
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            continue
        if status and fu.status != status:
            continue
        if chat_id and fu.chat_id != chat_id:
            continue
        if person_id and fu.person_id != person_id:
            continue
        if horizon is not None:
            due = parse_due(fu.due_at)
            if due is None or due > horizon:
                continue
        out.append(fu)
    # Earliest-due first.
    out.sort(key=lambda f: f.due_at or "")
    return out


def new_id() -> str:
    """Unique, url-safe id for a follow-up."""
    return f"fu_{uuid.uuid4().hex[:10]}"
