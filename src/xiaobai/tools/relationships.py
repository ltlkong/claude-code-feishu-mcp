"""Relationship store — person-centric, channel-agnostic.

Replaces the channel-bound ``profile.py`` model. A person (妈妈 / 姐姐 /
叔叔) is a single entity that may have multiple identities across
channels (Feishu ``ou_…``, WeChat ``…@im.wechat``, future iMessage
numbers, Telegram IDs, …). This module owns that aggregation.

Storage layout::

    workspace/state/relationships/
    ├── index.json            # {channel: {user_id: person_id}}
    └── {person_id}.md        # markdown + YAML frontmatter, one per person

The ``person_profile`` string surfaced to Claude on each inbound message
is the markdown body (no frontmatter) — that's what the model sees when
deciding how to talk to someone.

Core contract:

- ``resolve(channel, user_id) -> person_id | None``
- ``get_profile(person_id) -> str``                 # markdown body
- ``load_person(person_id) -> PersonRecord``       # structured read
- ``upsert_person(record, *, merge_identity=True)`` # write
- ``link_identity(channel, user_id, person_id)``    # add a new identity

Read paths are cached until the relevant file's mtime changes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_REL_DIR = _PROJECT_ROOT / "workspace" / "state" / "relationships"
_INDEX_FILE = _REL_DIR / "index.json"


# ── Data model ────────────────────────────────────────────────────

@dataclass
class PersonRecord:
    person_id: str
    display_name: str              # short alias used in chat ("妈妈", "叔叔")
    real_name: str = ""
    relation: str = ""             # free text: "Boss老婆", "Boss亲妈", "Boss姐姐"
    location: str = ""
    timezone: str = ""
    birthday: str = ""             # ISO-ish, optional
    phone: str = ""
    channels: dict[str, list[str]] = field(default_factory=dict)  # {"feishu": [...], "wechat": [...]}
    body: str = ""                 # free-form markdown notes (近况 / 偏好 / 互动)


# ── Index I/O ─────────────────────────────────────────────────────

_index_cache: dict[str, Any] = {}
_index_mtime: float = 0.0


def _load_index() -> dict[str, Any]:
    global _index_cache, _index_mtime
    if not _INDEX_FILE.is_file():
        return {"identities": {}, "persons": []}
    mtime = _INDEX_FILE.stat().st_mtime
    if mtime == _index_mtime and _index_cache:
        return _index_cache
    try:
        _index_cache = json.loads(_INDEX_FILE.read_text())
        _index_mtime = mtime
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("relationships index parse failed: %s", e)
        _index_cache = {"identities": {}, "persons": []}
    return _index_cache


def _save_index(idx: dict[str, Any]) -> None:
    global _index_cache, _index_mtime
    _REL_DIR.mkdir(parents=True, exist_ok=True)
    _INDEX_FILE.write_text(json.dumps(idx, ensure_ascii=False, indent=2))
    _index_cache = idx
    _index_mtime = _INDEX_FILE.stat().st_mtime


# ── Person file I/O ───────────────────────────────────────────────

def _person_path(person_id: str) -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_\-]", "_", person_id)
    return _REL_DIR / f"{safe}.md"


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _parse_person_file(text: str) -> tuple[dict, str]:
    """Split YAML-ish frontmatter from markdown body. Returns ``(meta, body)``."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text.strip()
    fm_raw, body = m.group(1), m.group(2).strip()
    meta: dict = {}
    try:
        import yaml  # optional; fall back to a toy parser if unavailable
        meta = yaml.safe_load(fm_raw) or {}
    except ImportError:
        meta = _toy_yaml_parse(fm_raw)
    return meta, body


def _toy_yaml_parse(text: str) -> dict:
    """Minimal YAML subset — flat keys + simple nested dicts + lists. No anchors."""
    out: dict = {}
    stack: list[tuple[int, Any]] = [(0, out)]  # (indent, container)
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        while stack and stack[-1][0] > indent:
            stack.pop()
        stripped = line.strip()
        if stripped.startswith("- "):
            container = stack[-1][1]
            if not isinstance(container, list):
                continue
            container.append(stripped[2:].strip().strip('"').strip("'"))
            continue
        if ":" not in stripped:
            continue
        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()
        parent = stack[-1][1]
        if not isinstance(parent, dict):
            continue
        if val == "":
            nested: dict | list = {}
            parent[key] = nested
            stack.append((indent + 2, nested))
        elif val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            parent[key] = (
                [x.strip().strip('"').strip("'") for x in inner.split(",") if x.strip()]
                if inner
                else []
            )
        else:
            parent[key] = val.strip('"').strip("'")
    return out


def _dump_person_file(record: PersonRecord) -> str:
    """Render a PersonRecord to frontmatter + body markdown."""
    fm_lines = [
        f"person_id: {record.person_id}",
        f"display_name: {record.display_name}",
    ]
    if record.real_name:
        fm_lines.append(f"real_name: {record.real_name}")
    if record.relation:
        fm_lines.append(f"relation: {record.relation}")
    if record.location:
        fm_lines.append(f"location: {record.location}")
    if record.timezone:
        fm_lines.append(f"timezone: {record.timezone}")
    if record.birthday:
        fm_lines.append(f"birthday: {record.birthday}")
    if record.phone:
        fm_lines.append(f"phone: \"{record.phone}\"")
    if record.channels:
        fm_lines.append("channels:")
        for channel, ids in record.channels.items():
            fm_lines.append(f"  {channel}:")
            for uid in ids:
                fm_lines.append(f"    - {uid}")
    fm = "\n".join(fm_lines)
    body = record.body.strip()
    return f"---\n{fm}\n---\n\n{body}\n" if body else f"---\n{fm}\n---\n"


def load_person(person_id: str) -> PersonRecord | None:
    path = _person_path(person_id)
    if not path.is_file():
        return None
    try:
        text = path.read_text()
    except OSError:
        return None
    meta, body = _parse_person_file(text)
    channels = meta.get("channels") or {}
    if not isinstance(channels, dict):
        channels = {}
    # Normalize: values must be lists
    channels = {k: (v if isinstance(v, list) else [v]) for k, v in channels.items() if v}
    return PersonRecord(
        person_id=str(meta.get("person_id", person_id)),
        display_name=str(meta.get("display_name", person_id)),
        real_name=str(meta.get("real_name", "")),
        relation=str(meta.get("relation", "")),
        location=str(meta.get("location", "")),
        timezone=str(meta.get("timezone", "")),
        birthday=str(meta.get("birthday", "")),
        phone=str(meta.get("phone", "")),
        channels=channels,
        body=body,
    )


def save_person(record: PersonRecord) -> Path:
    _REL_DIR.mkdir(parents=True, exist_ok=True)
    path = _person_path(record.person_id)
    path.write_text(_dump_person_file(record))
    return path


# ── Resolution (channel, user_id → person_id) ─────────────────────

def resolve(channel: str, user_id: str) -> str | None:
    """Return the ``person_id`` that owns ``(channel, user_id)``, or None."""
    if not channel or not user_id:
        return None
    idx = _load_index()
    ids = idx.get("identities", {}).get(channel, {})
    return ids.get(user_id)


def link_identity(channel: str, user_id: str, person_id: str) -> None:
    """Associate ``(channel, user_id)`` with ``person_id``.

    Updates both the index and the person file's ``channels`` block.
    """
    idx = _load_index()
    idx.setdefault("identities", {}).setdefault(channel, {})[user_id] = person_id
    persons = set(idx.setdefault("persons", []))
    persons.add(person_id)
    idx["persons"] = sorted(persons)
    _save_index(idx)

    # Update the person file's channels list
    record = load_person(person_id)
    if record is None:
        record = PersonRecord(person_id=person_id, display_name=person_id)
    ids = record.channels.setdefault(channel, [])
    if user_id not in ids:
        ids.append(user_id)
        save_person(record)


def upsert_person(record: PersonRecord) -> Path:
    """Write a PersonRecord and sync the index's identity map."""
    path = save_person(record)
    idx = _load_index()
    identities = idx.setdefault("identities", {})
    for channel, ids in record.channels.items():
        ch_map = identities.setdefault(channel, {})
        for uid in ids:
            ch_map[uid] = record.person_id
    persons = set(idx.setdefault("persons", []))
    persons.add(record.person_id)
    idx["persons"] = sorted(persons)
    _save_index(idx)
    return path


# ── Rendering for notification context ────────────────────────────

def get_profile(person_id: str) -> str:
    """Return a compact markdown snippet for the notification context.

    Header = display_name, relation, optional location.
    Body = trimmed notes (first ~300 chars is plenty for the model).
    """
    record = load_person(person_id)
    if record is None:
        return ""
    parts: list[str] = []
    header = f"**{record.display_name}**"
    if record.relation:
        header += f" ({record.relation})"
    details = ", ".join(p for p in [record.real_name, record.location, record.phone] if p)
    if details:
        header += f" — {details}"
    parts.append(header)
    if record.body:
        body = record.body.strip()
        if len(body) > 400:
            body = body[:400].rstrip() + "…"
        parts.append(body)
    return "\n".join(parts)


def person_context_for(channel: str, user_id: str) -> str:
    """Shortcut: resolve + render. Empty string if no person is linked."""
    pid = resolve(channel, user_id)
    if not pid:
        return ""
    return get_profile(pid)


# ── Debugging / introspection ─────────────────────────────────────

def list_persons() -> list[str]:
    idx = _load_index()
    return list(idx.get("persons", []))
