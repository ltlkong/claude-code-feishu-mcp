"""Heartbeat — per-chat proactive messaging that simulates a bored friend.

Each active chat gets its own independent heartbeat with randomized intervals.
Uses Claude Haiku via CLI with full user profiles and conversation context.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Where Claude Code stores conversation sessions for this project
_PROJECT_SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-Users-ltl-Workspace-bot-feishu-claude-code"
_PROFILES_DIR = Path(__file__).resolve().parent.parent.parent / "profiles"

# Heartbeat config
DEFAULT_MODEL = "haiku"
RECENT_MESSAGES = 30  # Look at last 30 messages (balance context vs speed)
MIN_INTERVAL_MINUTES = 15
MAX_INTERVAL_MINUTES = 60

# Chats to skip (bot removed, deleted, etc.) — loaded from file, updated dynamically
_BLACKLIST_FILE = Path(__file__).resolve().parent.parent.parent / "workspace" / "state" / "heartbeat_blacklist.txt"


def _load_blacklist() -> set[str]:
    """Load blacklisted chat IDs from file."""
    if _BLACKLIST_FILE.is_file():
        return {line.strip() for line in _BLACKLIST_FILE.read_text().splitlines() if line.strip() and not line.startswith("#")}
    return set()


def blacklist_chat(chat_id: str, reason: str = ""):
    """Add a chat to the heartbeat blacklist (e.g. when bot is removed from chat)."""
    _BLACKLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_blacklist()
    if chat_id not in existing:
        with open(_BLACKLIST_FILE, "a") as f:
            comment = f"  # {reason}" if reason else ""
            f.write(f"{chat_id}{comment}\n")
        logger.info("Heartbeat: blacklisted chat %s (%s)", chat_id[:12], reason)

# Per-chat state
_chat_states: dict[str, dict] = {}  # chat_id -> {last_hash, last_sent, next_check, last_activity}

# Inactivity threshold (per-chat)
_inactivity_threshold_minutes: int = 10


def configure_inactivity(minutes: int):
    """Set the inactivity threshold (called from server with config value)."""
    global _inactivity_threshold_minutes
    _inactivity_threshold_minutes = minutes


def mark_activity(chat_id: str = ""):
    """Call this when a message is received to reset the per-chat inactivity timer."""
    if chat_id:
        state = _get_chat_state(chat_id)
        state["last_activity"] = time.time()


def is_chat_inactive(chat_id: str) -> bool:
    """Check if a specific chat has been inactive for the threshold period."""
    state = _get_chat_state(chat_id)
    elapsed = time.time() - state.get("last_activity", 0)
    return elapsed >= _inactivity_threshold_minutes * 60


def _get_chat_state(chat_id: str) -> dict:
    """Get or create per-chat heartbeat state."""
    if chat_id not in _chat_states:
        # First check happens 3-5 minutes after discovery (not 15-60)
        _chat_states[chat_id] = {
            "last_hash": None,
            "last_sent": 0,  # timestamp of last heartbeat message sent
            "last_activity": 0,  # per-chat activity tracking (0 = no recent activity known)
            "next_check": time.time() + random.randint(3 * 60, 5 * 60),
        }
    return _chat_states[chat_id]


def _load_profiles_for_chat(chat_id: str, user_ids: set[str]) -> str:
    """Load all user profiles for a given chat."""
    if not _PROFILES_DIR.is_dir():
        return ""
    profiles = []
    for uid in sorted(user_ids):
        filepath = _PROFILES_DIR / f"{chat_id}_{uid}.md"
        if filepath.is_file():
            content = filepath.read_text().strip()
            if content:
                profiles.append(content)
    return "\n".join(profiles) if profiles else "(no profiles saved for this chat)"


def _read_chat_messages(chat_id: str, max_messages: int = RECENT_MESSAGES) -> tuple[str, set[str]]:
    """Read recent messages for a specific chat from the current session.

    Returns:
        (conversation_text, user_ids) — filtered to only this chat's messages.
    """
    if not _PROJECT_SESSIONS_DIR.exists():
        return "(No conversation history found)", set()

    session_files = sorted(
        _PROJECT_SESSIONS_DIR.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    if not session_files:
        return "(No conversation history found)", set()

    sf = session_files[0]
    entries = []
    user_ids: set[str] = set()

    try:
        lines = sf.read_text().strip().split("\n")
        # Track which assistant messages follow messages from this chat
        last_was_this_chat = False

        for line in lines:
            try:
                entry = json.loads(line)
                entry_type = entry.get("type", "")
                msg = entry.get("message", {})
                if not isinstance(msg, dict):
                    continue

                if entry_type == "user":
                    content = msg.get("content", "")
                    if isinstance(content, str) and content.strip():
                        text = content.strip()
                        cid = re.search(r'chat_id="([^"]+)"', text)
                        uid = re.search(r'user_id="([^"]+)"', text)
                        cur_chat_id = cid.group(1) if cid else None
                        cur_user_id = uid.group(1) if uid else None

                        if cur_chat_id == chat_id:
                            last_was_this_chat = True
                            if cur_user_id:
                                user_ids.add(cur_user_id)
                            # Extract message content
                            m = re.search(r"<channel[^>]*>(.*?)</channel>", text, re.DOTALL)
                            msg_text = m.group(1).strip() if m else text
                            if len(msg_text) > 500:
                                msg_text = msg_text[:500] + "..."
                            user_tag = f" ({cur_user_id})" if cur_user_id else ""
                            entries.append(f"[User{user_tag}]: {msg_text}")
                        else:
                            last_was_this_chat = False

                elif entry_type == "assistant" and last_was_this_chat:
                    # Only extract actual Feishu replies (MCP tool calls), not internal reasoning
                    content = msg.get("content", "")
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        if block.get("type") != "tool_use":
                            continue
                        tool_name = block.get("name", "")
                        tool_input = block.get("input", {})
                        # Only capture feishu reply-type tools targeting this chat
                        if not tool_name.startswith("mcp__feishu__"):
                            continue
                        tc = tool_input.get("chat_id", "")
                        if tc and tc != chat_id:
                            continue
                        if tool_name == "mcp__feishu__reply":
                            reply_text = tool_input.get("text", "")
                            if reply_text:
                                if len(reply_text) > 300:
                                    reply_text = reply_text[:300] + "..."
                                entries.append(f"[Me]: {reply_text}")
                        elif tool_name == "mcp__feishu__reply_card":
                            card_text = tool_input.get("text", "")
                            status = tool_input.get("status", "")
                            if card_text:
                                entries.append(f"[Me]: [card: {status}] {card_text[:200]}")
                        elif tool_name == "mcp__feishu__send_reaction":
                            emoji = tool_input.get("emoji", "")
                            entries.append(f"[Me]: [reacted {emoji}]")
                        elif tool_name == "mcp__feishu__reply_post":
                            entries.append(f"[Me]: [sent rich post]")
                        elif tool_name in ("mcp__feishu__reply_image", "mcp__feishu__reply_file",
                                           "mcp__feishu__reply_video", "mcp__feishu__reply_audio"):
                            media_type = tool_name.replace("mcp__feishu__reply_", "")
                            entries.append(f"[Me]: [sent {media_type}]")
                        elif tool_name == "mcp__feishu__update_profile":
                            pass  # skip profile updates, not conversation
                        elif tool_name in ("mcp__feishu__create_doc", "mcp__feishu__create_bitable"):
                            entries.append(f"[Me]: [created {tool_name.replace('mcp__feishu__create_', '')}]")
                        elif tool_name == "mcp__feishu__search_image":
                            entries.append(f"[Me]: [searched image: {tool_input.get('query', '')}]")

            except (json.JSONDecodeError, KeyError, TypeError):
                continue

    except Exception as e:
        logger.debug("Failed to read session %s: %s", sf.name, e)

    # Take last N entries
    recent = entries[-max_messages:]
    text = "\n".join(recent) if recent else "(No recent messages in this chat)"
    return text, user_ids


_HEARTBEAT_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "HEARTBEAT.md"


def _build_chat_prompt(chat_id: str) -> tuple[str, set[str]]:
    """Build a heartbeat prompt for a specific chat.

    Returns:
        (prompt_text, user_ids)
    """
    conversation, user_ids = _read_chat_messages(chat_id)

    try:
        instructions = _HEARTBEAT_PROMPT_PATH.read_text().strip()
    except FileNotFoundError:
        instructions = "You are 小白. Decide if you want to say something to this chat. Reply with MESSAGE:<text> or NO_MESSAGE."

    profiles = _load_profiles_for_chat(chat_id, user_ids)

    # Get current time info for context
    from datetime import datetime, timezone, timedelta
    utc_now = datetime.now(timezone.utc)
    pdt_now = utc_now - timedelta(hours=7)  # Vancouver PDT
    cst_now = utc_now + timedelta(hours=8)  # China CST
    time_context = f"Current time: Vancouver {pdt_now.strftime('%H:%M')}, China {cst_now.strftime('%H:%M')}"

    prompt = f"""{instructions}

---

Chat: {chat_id}
{time_context}

User profiles in this chat:
{profiles}

Recent conversation ({len(conversation.splitlines())} messages):
{conversation}

Your response:"""

    return prompt, user_ids


def run_chat_heartbeat(chat_id: str, model: str = DEFAULT_MODEL) -> str | None:
    """Run heartbeat for a single chat.

    Returns:
        message text or None if nothing to say.
    """
    state = _get_chat_state(chat_id)
    prompt, user_ids = _build_chat_prompt(chat_id)

    # Dedup: skip if context hasn't changed
    context_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    if context_hash == state["last_hash"]:
        logger.info("Heartbeat [%s]: context unchanged, skipping", chat_id[:8])
        return None
    state["last_hash"] = context_hash

    try:
        result = subprocess.run(
            ["claude", "--print", "--model", model, "--max-turns", "5"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )

        if result.returncode != 0:
            logger.warning("Heartbeat [%s] CLI failed: %s", chat_id[:8], result.stderr[:200])
            return None

        response = result.stdout.strip()

        if not response or "NO_MESSAGE" in response:
            logger.info("Heartbeat [%s]: Haiku says nothing, sending nudge anyway", chat_id[:8])
            return "NO_SUGGESTION"  # Still notify main session — it decides independently

        # Extract suggestion (Haiku suggests direction, main Claude composes)
        sug_match = re.search(r"SUGGESTION:\s*(.+)", response, re.DOTALL)
        if sug_match:
            suggestion = sug_match.group(1).strip()
        else:
            # Try MESSAGE format as fallback
            msg_match = re.search(r"MESSAGE:\s*(.+)", response, re.DOTALL)
            if msg_match:
                suggestion = msg_match.group(1).strip()
            else:
                suggestion = response.strip()
                if any(kw in suggestion.lower() for kw in ["no_message", "target:", "i don't", "i should"]):
                    return None

        # Sanity checks
        if len(suggestion) > 500:
            suggestion = suggestion[:500]
        if len(suggestion) < 2:
            return None

        logger.info("Heartbeat [%s]: suggestion (%d chars): %s", chat_id[:8], len(suggestion), suggestion[:80])
        state["last_sent"] = time.time()
        return suggestion

    except subprocess.TimeoutExpired:
        logger.warning("Heartbeat [%s]: Haiku timed out", chat_id[:8])
        return None
    except Exception as e:
        logger.error("Heartbeat [%s] error: %s", chat_id[:8], e)
        return None


def _get_active_chats() -> dict[str, set[str]]:
    """Scan current session to find all active chats and their users."""
    if not _PROJECT_SESSIONS_DIR.exists():
        return {}

    session_files = sorted(
        _PROJECT_SESSIONS_DIR.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not session_files:
        return {}

    sf = session_files[0]
    chat_users: dict[str, set[str]] = {}

    try:
        lines = sf.read_text().strip().split("\n")
        for line in lines:
            try:
                entry = json.loads(line)
                if entry.get("type") != "user":
                    continue
                content = entry.get("message", {}).get("content", "")
                if not isinstance(content, str):
                    continue
                cid = re.search(r'chat_id="([^"]+)"', content)
                uid = re.search(r'user_id="([^"]+)"', content)
                if cid:
                    cid_val = cid.group(1)
                    if cid_val not in chat_users:
                        chat_users[cid_val] = set()
                    if uid:
                        chat_users[cid_val].add(uid.group(1))
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
    except Exception as e:
        logger.debug("Failed to scan session: %s", e)

    return chat_users


async def heartbeat_loop(send_fn, interval_minutes: int = 5, model: str = DEFAULT_MODEL,
                         notify_fn=None):
    """Background loop that runs per-chat heartbeats.

    Polls every 5 minutes. For each active chat, checks if it's time
    to run a heartbeat (randomized interval per chat). Only runs when
    there's been no recent activity (inactivity threshold).

    Two modes:
    - If notify_fn is provided: sends suggestion as notification to main Claude session
      (Claude decides whether to act on it — double-filtering)
    - If no notify_fn: falls back to send_fn (direct message)

    Args:
        send_fn: async function(chat_id, text) to send a Feishu message (fallback)
        interval_minutes: Polling interval in minutes (default 5)
        model: Claude model to use (default "haiku")
        notify_fn: async function(content, meta) to notify main Claude session
    """
    poll_seconds = interval_minutes * 60
    logger.info("Heartbeat started: per-chat mode, inactivity_threshold=%dm, interval=%d-%dm, model=%s, notify=%s",
                _inactivity_threshold_minutes, MIN_INTERVAL_MINUTES, MAX_INTERVAL_MINUTES, model,
                "yes" if notify_fn else "no (direct send)")

    # Wait a bit before first check (short delay to let system settle)
    await asyncio.sleep(30)

    while True:
        try:
            # Get all active chats from session
            active_chats = _get_active_chats()
            if not active_chats:
                logger.info("Heartbeat: no active chats found")
                await asyncio.sleep(poll_seconds)
                continue

            now = time.time()
            checked_any = False

            for chat_id, user_ids in active_chats.items():
                if chat_id in _load_blacklist():
                    continue

                state = _get_chat_state(chat_id)

                # Per-chat inactivity check
                if not is_chat_inactive(chat_id):
                    logger.info("Heartbeat [%s]: chat active (%.0fs ago), skip",
                                chat_id[:8], now - state.get("last_activity", 0))
                    continue

                # Check if it's time for this chat's heartbeat
                if now < state["next_check"]:
                    logger.info("Heartbeat [%s]: not yet (%.0fs until next check)",
                                chat_id[:8], state["next_check"] - now)
                    continue

                # Don't spam — at least 15 min since last sent message to this chat
                if now - state["last_sent"] < MIN_INTERVAL_MINUTES * 60:
                    continue

                logger.info("Heartbeat: running check for chat %s", chat_id[:12])

                loop = asyncio.get_running_loop()
                suggestion = await loop.run_in_executor(None, run_chat_heartbeat, chat_id, model)

                # Always notify main session — it decides independently whether to act
                if suggestion and notify_fn:
                    content = f"Heartbeat for chat {chat_id}. Haiku suggestion: {suggestion}"
                    meta = {
                        "source": "heartbeat",
                        "chat_id": chat_id,
                        "suggestion": suggestion,
                    }
                    await notify_fn(content, meta)
                    logger.info("Heartbeat: notified main session for %s: %s", chat_id[:12], suggestion[:80])

                # Schedule next check with random interval
                state["next_check"] = now + random.randint(MIN_INTERVAL_MINUTES * 60, MAX_INTERVAL_MINUTES * 60)

                # Small delay between chats to avoid burst
                await asyncio.sleep(2)

        except Exception as e:
            logger.error("Heartbeat loop error: %s", e)

        await asyncio.sleep(poll_seconds)
