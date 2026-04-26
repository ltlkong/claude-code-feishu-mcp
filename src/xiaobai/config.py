"""Unified settings for the xiaobai bot.

Merges the legacy ``feishu_channel.config.Settings`` and
``wechat_channel.config.WeChatSettings`` into one class. All env-var names
are preserved so existing ``.env`` files continue to work when Session 3
flips the entry point.

Also exposes ``load_instructions()`` — returns CLAUDE.md's contents from the
project root, with a minimal fallback string.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# Project root = 3 levels above this file (src/xiaobai/config.py)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Unified configuration for Feishu + WeChat channels."""

    # ── Feishu app credentials ──────────────────────────────────
    feishu_app_id: str = ""
    feishu_app_secret: str = ""

    # Sender gating — empty list means allow all
    allowed_user_ids: list[str] = []

    # ── ElevenLabs (optional) ───────────────────────────────────
    # Enables reply_audio tool and voice message transcription
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""

    # ── Paths ────────────────────────────────────────────────────
    temp_dir: Path = Path("/tmp/feishu-channel")
    wechat_temp_dir: Path = Path("/tmp/wechat-channel")
    state_dir: Path = Path("workspace/state")

    # ── Cleanup ──────────────────────────────────────────────────
    temp_file_max_age_hours: int = 2
    stale_card_timeout_minutes: int = 30

    # ── Heartbeat ────────────────────────────────────────────────
    heartbeat_interval_minutes: int = 15
    heartbeat_inactivity_minutes: int = 5

    # ── Image search APIs ────────────────────────────────────────
    pexels_api_key: str = ""
    tenor_api_key: str = ""

    # ── WeChat iLink ─────────────────────────────────────────────
    ilink_base_url: str = "https://ilinkai.weixin.qq.com"
    ilink_cdn_url: str = "https://novac2c.cdn.weixin.qq.com/c2c"
    wechat_qr_notify_chat_id: str = ""

    # ── Model provider ───────────────────────────────────────────
    xiaobai_provider: str = "claude"
    cursor_command: str = "cursor-agent"
    cursor_args: str = ""
    cursor_prompt_flag: str = "-p"
    cursor_timeout_seconds: int = 120

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Helpers ──────────────────────────────────────────────────

    def load_instructions(self) -> str:
        """Load CLAUDE.md contents (the bot's persona / behavioral rules).

        Falls back to a neutral default when the file is missing.
        """
        path = _PROJECT_ROOT / "CLAUDE.md"
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return (
            "You are a helpful assistant on a chat channel. "
            "Respond to messages and use the provided tools."
        )
