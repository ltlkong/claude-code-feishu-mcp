"""Heartbeat — periodically review conversation history and proactively message the boss.

Uses Claude Haiku via CLI to review recent conversation context and memory,
then decides whether there's anything worth proactively sharing. Only sends
a message when there's something meaningful to say.
"""

import asyncio
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


def _read_current_session(max_messages: int = RECENT_MESSAGES) -> tuple[str, str | None]:
    """Read the last N messages from the current (most recent) session.

    Returns:
        (conversation_text, last_chat_id) — last_chat_id is extracted from the
        most recent Feishu channel message, or None if not found.
    """
    if not _PROJECT_SESSIONS_DIR.exists():
        return "(No conversation history found)", None

    session_files = sorted(
        _PROJECT_SESSIONS_DIR.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )

    if not session_files:
        return "(No conversation history found)", None

    # Only read the most recent session (current session)
    sf = session_files[0]
    snippets = []
    last_chat_id: str | None = None

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
                        # Strip Feishu channel XML tags to get the actual text
                        text = content.strip()
                        # Extract chat_id from channel tag
                        cid = re.search(r'chat_id="([^"]+)"', text)
                        if cid:
                            last_chat_id = cid.group(1)
                        # Extract text between channel tags if present
                        m = re.search(r"<channel[^>]*>(.*?)</channel>", text, re.DOTALL)
                        if m:
                            text = m.group(1).strip()
                        if len(text) > 500:
                            text = text[:500] + "..."
                        entries.append(f"[User]: {text}")

                elif entry_type == "assistant":
                    content = msg.get("content", "")
                    # Content can be a string or a list of content blocks
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
    return text, last_chat_id


_HEARTBEAT_PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "HEARTBEAT.md"


def _build_heartbeat_prompt() -> tuple[str, str | None]:
    """Build the prompt for Haiku. Reads instructions from HEARTBEAT.md, appends recent context.

    Returns:
        (prompt_text, chat_id) — chat_id auto-detected from the most recent Feishu message.
    """
    recent_context, chat_id = _read_current_session()

    try:
        instructions = _HEARTBEAT_PROMPT_PATH.read_text().strip()
    except FileNotFoundError:
        instructions = "Review the conversation below. If there's anything worth proactively telling the boss, say it in Chinese. Otherwise respond with NO_MESSAGE."

    prompt = f"""{instructions}

Recent messages (current session):
{recent_context}

Your response (either NO_MESSAGE or the proactive message):"""

    return prompt, chat_id


def run_heartbeat(model: str = DEFAULT_MODEL) -> tuple[str | None, str | None]:
    """Run a single heartbeat check.

    Returns:
        (message_text, chat_id) — message is None if nothing to say,
        chat_id is auto-detected from the session.
    """
    prompt, chat_id = _build_heartbeat_prompt()

    if not chat_id:
        logger.debug("Heartbeat: no chat_id found in session, skipping")
        return None, None

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
            return None

        response = result.stdout.strip()

        if not response or "NO_MESSAGE" in response:
            logger.debug("Heartbeat: nothing to say")
            return None, chat_id

        # Sanity check — don't send if response looks like an error or is too long
        if len(response) > 1000:
            response = response[:1000]

        logger.info("Heartbeat: proactive message generated (%d chars)", len(response))
        return response, chat_id

    except subprocess.TimeoutExpired:
        logger.warning("Heartbeat: Haiku timed out")
        return None, chat_id
    except Exception as e:
        logger.error("Heartbeat error: %s", e)
        return None, chat_id


async def heartbeat_loop(send_fn, interval_minutes: int = DEFAULT_INTERVAL_MINUTES, model: str = DEFAULT_MODEL):
    """Background loop that periodically checks if there's something to proactively say.

    The target chat_id is auto-detected from the most recent Feishu message
    in the conversation session — no manual configuration needed.

    Args:
        send_fn: async function(chat_id, text) to send a Feishu message
        interval_minutes: How often to check (default 60 min)
        model: Claude model to use (default "haiku")
    """
    interval_seconds = interval_minutes * 60
    logger.info("Heartbeat started: interval=%dm, model=%s, chat_id=auto", interval_minutes, model)

    # Wait a bit before first check (let the system settle)
    await asyncio.sleep(120)

    while True:
        try:
            # Run Haiku check in a thread to not block the event loop
            loop = asyncio.get_running_loop()
            message, chat_id = await loop.run_in_executor(None, run_heartbeat, model)

            if message and chat_id:
                await send_fn(chat_id, message)
                logger.info("Heartbeat: sent proactive message to %s", chat_id)

        except Exception as e:
            logger.error("Heartbeat loop error: %s", e)

        await asyncio.sleep(interval_seconds)
