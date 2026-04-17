"""Reminder management — create, list, delete cron-based Feishu reminders.

Supports two types:
- **Simple**: cron → send a fixed message directly via Feishu API
- **Smart**: cron → write a task file → bot picks it up → Claude thinks → decides response

Supports optional execution limits (max_runs) — auto-deletes cron after N executions.

CLI usage (called by cron):
    python -m xiaobai.reminders_cli send <chat_id> <message>
    python -m xiaobai.reminders_cli trigger <chat_id> <prompt>
    python -m xiaobai.reminders_cli limit <reminder_id> <max_runs> <subcommand...>

Ported from ``feishu_channel.reminder`` in Session 3. The CRON_TAG stays as
``# feishu-reminder:`` so that pre-migration crontab entries remain parseable
by ``list_reminders`` / ``delete_reminder`` during the cutover window.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Tag used to identify our cron entries (unchanged for backwards compat)
CRON_TAG = "# feishu-reminder:"

# Paths (unchanged — legacy scheduled tasks already sit here)
COUNTER_DIR = Path("/tmp/feishu-channel/reminder_counts")
SCHEDULED_DIR = Path("/tmp/feishu-channel/scheduled")

# For cron commands, we call `python -m xiaobai.reminders_cli <subcmd>`
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
PYTHON = _PROJECT_ROOT / "venv" / "bin" / "python"
_MODULE = "xiaobai.reminders_cli"


# ── Feishu message sending (used by cron) ────────────────────────

def _send_feishu_message(chat_id: str, text: str) -> dict:
    """Send a text message to a Feishu chat. Reads credentials from .env."""
    from .config import Settings
    settings = Settings(_env_file=_PROJECT_ROOT / ".env")

    with httpx.Client() as client:
        # Get tenant token
        resp = client.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": settings.feishu_app_id, "app_secret": settings.feishu_app_secret},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to get token: {data}")
        token = data["tenant_access_token"]

        # Send message
        resp = client.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "receive_id": chat_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
        )
        return resp.json()


# ── Smart task triggering (used by cron) ─────────────────────────

def _trigger_smart_task(chat_id: str, prompt: str) -> str:
    """Write a task file for the bot to pick up and let Claude handle."""
    SCHEDULED_DIR.mkdir(parents=True, exist_ok=True)
    task_id = f"{int(time.time())}_{uuid.uuid4().hex[:8]}"
    task = {
        "task_id": task_id,
        "chat_id": chat_id,
        "prompt": prompt,
        "created_at": time.time(),
    }
    path = SCHEDULED_DIR / f"{task_id}.json"
    path.write_text(json.dumps(task, ensure_ascii=False))
    return str(path)


# ── Execution count limiter (used by cron) ───────────────────────

def _get_counter(reminder_id: str, initial: int) -> int:
    """Get remaining count. Creates counter file with initial value if not exists."""
    COUNTER_DIR.mkdir(parents=True, exist_ok=True)
    counter_file = COUNTER_DIR / f"{reminder_id}.count"
    if not counter_file.exists():
        counter_file.write_text(str(initial))
        return initial
    try:
        return int(counter_file.read_text().strip())
    except (ValueError, OSError):
        return 0


def _set_counter(reminder_id: str, count: int):
    counter_file = COUNTER_DIR / f"{reminder_id}.count"
    counter_file.write_text(str(count))


def _delete_cron(reminder_id: str):
    """Remove this reminder's cron entry."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode != 0:
            return
        lines = result.stdout.splitlines(True)
        new_lines = [l for l in lines if CRON_TAG not in l or reminder_id not in l]
        subprocess.run(["crontab", "-"], input="".join(new_lines), text=True, check=True)
    except Exception as e:
        print(f"Failed to remove cron: {e}", file=sys.stderr)


def _cleanup_counter(reminder_id: str):
    counter_file = COUNTER_DIR / f"{reminder_id}.count"
    if counter_file.exists():
        counter_file.unlink()


def _run_with_limit(reminder_id: str, initial_count: int, command: list[str]):
    """Run a command with execution count limit. Auto-deletes cron when exhausted."""
    remaining = _get_counter(reminder_id, initial_count)

    if remaining <= 0:
        _delete_cron(reminder_id)
        _cleanup_counter(reminder_id)
        return

    remaining -= 1
    _set_counter(reminder_id, remaining)

    result = subprocess.run(command)

    if remaining <= 0:
        _delete_cron(reminder_id)
        _cleanup_counter(reminder_id)
        print(f"Last execution done for '{reminder_id}'.")

    sys.exit(result.returncode)


# ── Crontab management (used by MCP tools) ───────────────────────

def _get_crontab() -> str:
    """Read current user crontab."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if result.returncode != 0:
            return ""
        return result.stdout
    except Exception:
        return ""


def _set_crontab(content: str) -> None:
    """Write user crontab."""
    subprocess.run(["crontab", "-"], input=content, text=True, check=True)


def _line_matches_id(line: str, reminder_id: str) -> bool:
    """Check if a crontab line belongs to a specific reminder ID."""
    if CRON_TAG not in line:
        return False
    tag_idx = line.index(CRON_TAG)
    tag_content = line[tag_idx + len(CRON_TAG):].strip()
    parsed_id = tag_content.split("|", 1)[0]
    return parsed_id == reminder_id


def _utc_cron_to_local(cron_expr: str) -> str:
    """Convert a UTC cron expression to system local timezone.

    Only converts when hour and minute are specific numbers (not wildcards/ranges).
    Handles day/month/day-of-week rollover when the hour shift crosses midnight.
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return cron_expr

    minute, hour, day, month, dow = fields

    # Only convert if hour is a specific number
    if not hour.isdigit() or not minute.isdigit():
        return cron_expr

    # Build a reference UTC datetime for conversion
    now = datetime.now(timezone.utc)
    try:
        ref_utc = now.replace(
            hour=int(hour), minute=int(minute), second=0, microsecond=0,
            day=int(day) if day.isdigit() else now.day,
            month=int(month) if month.isdigit() else now.month,
        )
    except ValueError:
        return cron_expr

    # Convert to system local timezone
    ref_local = ref_utc.astimezone()

    new_minute = str(ref_local.minute)
    new_hour = str(ref_local.hour)
    new_day = str(ref_local.day) if day.isdigit() else day
    new_month = str(ref_local.month) if month.isdigit() else month

    # Day-of-week: cron uses 0=Sunday, Python weekday() uses 0=Monday
    if dow.isdigit():
        new_dow = str((ref_local.weekday() + 1) % 7)
    else:
        new_dow = dow

    return f"{new_minute} {new_hour} {new_day} {new_month} {new_dow}"


def create_reminder(reminder_id: str, cron_expr: str, chat_id: str, message: str,
                    smart: bool = False, max_runs: int = 0) -> dict:
    """Add a cron entry for a scheduled reminder.

    Args:
        reminder_id: Unique identifier for this reminder (e.g. "morning_standup")
        cron_expr: Cron expression in UTC (e.g. "0 1 * * *" for daily 1am UTC).
                   Automatically converted to system local timezone before writing to crontab.
        chat_id: Feishu chat_id to send to
        message: For simple: the message text. For smart: the prompt for Claude.
        smart: If True, triggers Claude to think instead of sending a fixed message.
        max_runs: Max execution count. 0 = unlimited (default). After N runs, cron auto-deletes.
    """
    if not re.match(r'^[\d\s\*,\-/]+$', cron_expr.strip()):
        return {"status": "error", "message": "Invalid characters in cron expression"}
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return {"status": "error", "message": f"Invalid cron expression: need 5 fields, got {len(fields)}"}

    # Convert UTC cron to system local timezone for macOS crontab
    local_cron = _utc_cron_to_local(cron_expr)
    logger.info("Cron timezone conversion: UTC %s -> local %s", cron_expr, local_cron)

    import shlex
    # Validate IDs to prevent shell injection
    if not re.match(r'^[a-zA-Z0-9_\-]+$', reminder_id):
        return {"status": "error", "message": f"Invalid reminder_id: {reminder_id}"}
    if not re.match(r'^[a-zA-Z0-9_\-]+$', chat_id):
        return {"status": "error", "message": f"Invalid chat_id: {chat_id}"}
    safe_message = shlex.quote(message)
    subcmd = "trigger" if smart else "send"
    mode_label = "smart" if smart else "simple"

    # Build the actual command: python -m xiaobai.reminders_cli send/trigger <chat_id> '<message>'
    actual_cmd = f"cd {_PROJECT_ROOT} && {PYTHON} -m {_MODULE} {subcmd} {chat_id} {safe_message}"

    if max_runs > 0:
        # Wrap with limit: python -m xiaobai.reminders_cli limit <id> <count> <actual_cmd...>
        cmd = f"cd {_PROJECT_ROOT} && {PYTHON} -m {_MODULE} limit {reminder_id} {max_runs} {PYTHON} -m {_MODULE} {subcmd} {chat_id} {safe_message}"
        mode_label += f"|max:{max_runs}"
        COUNTER_DIR.mkdir(parents=True, exist_ok=True)
        (COUNTER_DIR / f"{reminder_id}.count").write_text(str(max_runs))
    else:
        cmd = actual_cmd

    cron_line = f"{local_cron} {cmd} {CRON_TAG}{reminder_id}|{mode_label}|utc:{cron_expr}\n"

    existing = _get_crontab()
    lines = [l for l in existing.splitlines(True) if not _line_matches_id(l, reminder_id)]
    lines.append(cron_line)

    _set_crontab("".join(lines))
    logger.info("Created %s reminder %s: %s -> %s (max_runs=%d)", mode_label, reminder_id, cron_expr, message, max_runs)

    result = {"status": "ok", "reminder_id": reminder_id, "cron": cron_expr, "message": message, "smart": smart}
    if max_runs > 0:
        result["max_runs"] = max_runs
    return result


def list_reminders() -> dict:
    """List all feishu-reminder cron entries."""
    existing = _get_crontab()
    reminders = []
    for line in existing.splitlines():
        if CRON_TAG not in line:
            continue
        tag_idx = line.index(CRON_TAG)
        tag_content = line[tag_idx + len(CRON_TAG):].strip()

        parts = tag_content.split("|")
        reminder_id = parts[0]
        mode = parts[1] if len(parts) > 1 else "simple"

        max_runs = 0
        remaining = None
        utc_cron = None
        for p in parts[2:]:
            if p.startswith("max:"):
                max_runs = int(p.split(":")[1])
            elif p.startswith("utc:"):
                utc_cron = p[4:]

        if max_runs > 0:
            counter_file = COUNTER_DIR / f"{reminder_id}.count"
            if counter_file.exists():
                try:
                    remaining = int(counter_file.read_text().strip())
                except (ValueError, OSError):
                    remaining = 0

        cron_part = line[:tag_idx].strip()
        cron_fields = cron_part.split(None, 5)
        cron_expr = " ".join(cron_fields[:5]) if len(cron_fields) >= 5 else "?"

        msg_match = re.search(r"'(.+?)'", cron_part)
        message = msg_match.group(1) if msg_match else "?"

        entry = {
            "id": reminder_id,
            "cron": utc_cron or cron_expr,
            "cron_local": cron_expr,
            "message": message,
            "smart": mode == "smart",
        }
        if max_runs > 0:
            entry["max_runs"] = max_runs
            entry["remaining"] = remaining

        reminders.append(entry)

    return {"status": "ok", "reminders": reminders, "count": len(reminders)}


def delete_reminder(reminder_id: str) -> dict:
    """Remove a reminder by ID."""
    existing = _get_crontab()
    lines = existing.splitlines(True)
    new_lines = [l for l in lines if not _line_matches_id(l, reminder_id)]

    if len(new_lines) == len(lines):
        return {"status": "error", "message": f"Reminder '{reminder_id}' not found"}

    _set_crontab("".join(new_lines))

    counter_file = COUNTER_DIR / f"{reminder_id}.count"
    if counter_file.exists():
        counter_file.unlink()

    logger.info("Deleted reminder %s", reminder_id)
    return {"status": "ok", "deleted": reminder_id}


# ── CLI entry point (called by cron) ─────────────────────────────

def main():
    """CLI dispatcher for cron-invoked subcommands."""
    if len(sys.argv) < 2:
        print(f"Usage: python -m {_MODULE} <send|trigger|limit> ...", file=sys.stderr)
        sys.exit(1)

    subcmd = sys.argv[1]

    if subcmd == "send":
        if len(sys.argv) < 4:
            print(f"Usage: python -m {_MODULE} send <chat_id> <message>", file=sys.stderr)
            sys.exit(1)
        chat_id = sys.argv[2]
        message = " ".join(sys.argv[3:])
        result = _send_feishu_message(chat_id, message)
        if result.get("code") == 0:
            print(f"Sent to {chat_id}: {message}")
        else:
            print(f"Failed: {result}", file=sys.stderr)
            sys.exit(1)

    elif subcmd == "trigger":
        if len(sys.argv) < 4:
            print(f"Usage: python -m {_MODULE} trigger <chat_id> <prompt>", file=sys.stderr)
            sys.exit(1)
        chat_id = sys.argv[2]
        prompt = " ".join(sys.argv[3:])
        path = _trigger_smart_task(chat_id, prompt)
        print(f"Task written: {path}")

    elif subcmd == "limit":
        if len(sys.argv) < 5:
            print(f"Usage: python -m {_MODULE} limit <reminder_id> <max_runs> <command...>", file=sys.stderr)
            sys.exit(1)
        reminder_id = sys.argv[2]
        initial_count = int(sys.argv[3])
        command = sys.argv[4:]
        _run_with_limit(reminder_id, initial_count, command)

    else:
        print(f"Unknown subcommand: {subcmd}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
