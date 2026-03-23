"""Heartbeat — periodically review conversation history and proactively message.

Uses Claude Haiku via CLI to review recent conversation context and memory,
then decides whether there's anything worth proactively sharing. Dynamically
routes messages to the appropriate chat and/or user based on context.
"""

import asyncio
import hashlib
import json
import logging
import re
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Where Claude Code stores conversation sessions for this project
_PROJECT_SESSIONS_DIR = Path.home() / ".claude" / "projects" / "-Users-ltl-Workspace-bot-feishu-claude-code"

# Heartbeat config
DEFAULT_INTERVAL_MINUTES = 60
DEFAULT_MODEL = "haiku"
RECENT_MESSAGES = 20  # Only look at the last N messages from current session

# Track the hash of the last processed messages to avoid re-processing
_last_context_hash: str | None = None


def _read_current_session(max_messages: int = RECENT_MESSAGES) -> tuple[str, dict[str, set[str]], str | None]:
    """Read the last N messages from the current (most recent) session.

    Returns:
        (conversation_text, chat_users, last_chat_id)
        - conversation_text: formatted recent messages with chat context
        - chat_users: {chat_id: {user_id, ...}} mapping of active conversations
        - last_chat_id: fallback chat_id from the most recent message
    """
    if not _PROJECT_SESSIONS_DIR.exists():
        return "(No conversation history found)", {}, None

    session_files = sorted(
        _PROJECT_SESSIONS_DIR.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    if not session_files:
        return "(No conversation history found)", {}, None

    # Only read the most recent session (current session)
    sf = session_files[0]
    snippets = []
    last_chat_id: str | None = None
    chat_users: dict[str, set[str]] = {}  # chat_id -> {user_ids}

    try:
        lines = sf.read_text().strip().split("\n")
        entries = []
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
                        # Extract chat_id and user_id from channel tag
                        cid = re.search(r'chat_id="([^"]+)"', text)
                        uid = re.search(r'user_id="([^"]+)"', text)
                        sender = re.search(r'sender_name="([^"]+)"', text)

                        cur_chat_id = cid.group(1) if cid else None
                        cur_user_id = uid.group(1) if uid else None

                        if cur_chat_id:
                            last_chat_id = cur_chat_id
                            if cur_chat_id not in chat_users:
                                chat_users[cur_chat_id] = set()
                            if cur_user_id:
                                chat_users[cur_chat_id].add(cur_user_id)

                        # Build annotated entry with chat context
                        chat_tag = f" [chat:{cur_chat_id}]" if cur_chat_id else ""
                        user_tag = f" (user:{cur_user_id})" if cur_user_id else ""

                        # Extract text between channel tags if present
                        m = re.search(r"<channel[^>]*>(.*?)</channel>", text, re.DOTALL)
                        if m:
                            text = m.group(1).strip()
                        if len(text) > 500:
                            text = text[:500] + "..."
                        entries.append(f"[User{user_tag}{chat_tag}]: {text}")

                elif entry_type == "assistant":
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
                        text = " ".join(t for t in texts if t.strip())
                    elif isinstance(content, str):
                        text = content
                    else:
                        continue
                    if text.strip() and text.strip() != "(Waiting for user response)":
                        if len(text) > 300:
                            text = text[:300] + "..."
                        entries.append(f"[Assistant]: {text}")

            except (json.JSONDecodeError, KeyError, TypeError):
                continue

        snippets = entries[-max_messages:]
    except Exception as e:
        logger.debug("Failed to read session %s: %s", sf.name, e)

    text = "\n".join(snippets) if snippets else "(No readable conversation history)"
    return text, chat_users, last_chat_id


_HEARTBEAT_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "HEARTBEAT.md"


def _build_heartbeat_prompt() -> tuple[str, dict[str, set[str]], str | None]:
    """Build the prompt for Haiku with multi-chat routing context.

    Returns:
        (prompt_text, chat_users, fallback_chat_id)
    """
    recent_context, chat_users, fallback_chat_id = _read_current_session()

    try:
        instructions = _HEARTBEAT_PROMPT_PATH.read_text().strip()
    except FileNotFoundError:
        instructions = "Review the conversation below. If there's anything worth proactively telling the boss, say it in Chinese. Otherwise respond with NO_MESSAGE."

    # Build active chats summary
    chats_summary = ""
    if chat_users:
        chat_lines = []
        for cid, uids in chat_users.items():
            users_str = ", ".join(sorted(uids)) if uids else "unknown"
            chat_lines.append(f"  - chat_id={cid} | users: {users_str}")
        chats_summary = "Active conversations:\n" + "\n".join(chat_lines)

    prompt = f"""{instructions}

{chats_summary}

Recent messages (current session):
{recent_context}

IMPORTANT: Your response format must be one of:
1. NO_MESSAGE — if nothing to say
2. TARGET:<chat_id>
   MESSAGE:<your message>

Choose the most appropriate chat_id from the active conversations above based on context.
If your message is relevant to a specific conversation, target that chat.
Your response:"""

    return prompt, chat_users, fallback_chat_id


def _parse_heartbeat_response(response: str, chat_users: dict[str, set[str]], fallback_chat_id: str | None) -> tuple[str | None, str | None]:
    """Parse Haiku's response to extract target and message.

    Returns:
        (message_text, chat_id) — message is None if nothing to say.
    """
    if not response or "NO_MESSAGE" in response:
        return None, None

    # Try structured format: TARGET:<chat_id>\nMESSAGE:<text>
    target_match = re.search(r"TARGET:\s*(\S+)", response)
    message_match = re.search(r"MESSAGE:\s*(.+)", response, re.DOTALL)

    if target_match and message_match:
        target_chat_id = target_match.group(1).strip()
        message = message_match.group(1).strip()

        # Validate that target is a known chat
        if target_chat_id in chat_users:
            return message, target_chat_id
        else:
            # Unknown target, fall back to the extracted chat_id
            logger.warning("Heartbeat: unknown target %s, using fallback", target_chat_id)
            return message, fallback_chat_id

    # Fallback: treat entire response as message, send to last chat
    message = response.strip()
    return message, fallback_chat_id


def _compute_context_hash(context: str) -> str:
    """Compute a hash of the conversation context for dedup."""
    return hashlib.sha256(context.encode()).hexdigest()[:16]


def run_heartbeat(model: str = DEFAULT_MODEL) -> tuple[str | None, str | None]:
    """Run a single heartbeat check.

    Returns:
        (message_text, chat_id) — message is None if nothing to say.
    """
    global _last_context_hash

    prompt, chat_users, fallback_chat_id = _build_heartbeat_prompt()

    if not chat_users and not fallback_chat_id:
        logger.debug("Heartbeat: no active chats found in session, skipping")
        return None, None

    # Dedup: skip if the last 20 messages haven't changed since last check
    context_hash = _compute_context_hash(prompt)
    if context_hash == _last_context_hash:
        logger.debug("Heartbeat: context unchanged (hash=%s), skipping", context_hash)
        return None, None
    _last_context_hash = context_hash

    try:
        result = subprocess.run(
            ["claude", "--print", "--model", model, "--max-turns", "1"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).resolve().parent.parent.parent),
        )

        if result.returncode != 0:
            logger.warning("Heartbeat CLI failed: %s", result.stderr[:200])
            return None, None

        response = result.stdout.strip()
        message, chat_id = _parse_heartbeat_response(response, chat_users, fallback_chat_id)

        if not message:
            logger.debug("Heartbeat: nothing to say")
            return None, None

        # Sanity check — don't send if response is too long
        if len(message) > 1000:
            message = message[:1000]

        logger.info("Heartbeat: proactive message generated (%d chars) -> %s", len(message), chat_id)
        return message, chat_id

    except subprocess.TimeoutExpired:
        logger.warning("Heartbeat: Haiku timed out")
        return None, None
    except Exception as e:
        logger.error("Heartbeat error: %s", e)
        return None, None


async def heartbeat_loop(send_fn, interval_minutes: int = DEFAULT_INTERVAL_MINUTES, model: str = DEFAULT_MODEL):
    """Background loop that periodically checks if there's something to proactively say.

    Dynamically routes messages to the appropriate chat based on conversation context.

    Args:
        send_fn: async function(chat_id, text) to send a Feishu message
        interval_minutes: How often to check (default 60 min)
        model: Claude model to use (default "haiku")
    """
    interval_seconds = interval_minutes * 60
    logger.info("Heartbeat started: interval=%dm, model=%s, routing=dynamic", interval_minutes, model)

    # Wait a bit before first check (let the system settle)
    await asyncio.sleep(120)

    while True:
        try:
            loop = asyncio.get_running_loop()
            message, chat_id = await loop.run_in_executor(None, run_heartbeat, model)

            if message and chat_id:
                await send_fn(chat_id, message)
                logger.info("Heartbeat: sent proactive message to %s", chat_id)

        except Exception as e:
            logger.error("Heartbeat loop error: %s", e)

        await asyncio.sleep(interval_seconds)
