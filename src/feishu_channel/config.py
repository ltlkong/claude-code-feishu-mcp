from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Channel configuration. All values can be set via environment variables or .env file."""

    # Feishu app credentials
    feishu_app_id: str
    feishu_app_secret: str

    # Sender gating — empty list means allow all
    allowed_user_ids: list[str] = []

    # ElevenLabs (optional — enables reply_audio tool and audio transcription)
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""

    # Paths
    temp_dir: Path = Path("/tmp/feishu-channel")

    # Cleanup
    temp_file_max_age_hours: int = 2
    stale_card_timeout_minutes: int = 30

    # Heartbeat — proactive messaging based on conversation history
    # chat_id is auto-detected from the most recent Feishu message in the session
    heartbeat_model: str = "haiku"
    heartbeat_interval_minutes: int = 60
    heartbeat_inactivity_minutes: int = 30

    # Pexels image search API (optional — enables search_image photo type)
    pexels_api_key: str = ""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
