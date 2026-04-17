"""Download/upload helpers for Feishu media + ElevenLabs TTS/transcription.

Ported verbatim from ``feishu_channel/media.py``. Logic is identical; only
the location of ``get_tenant_token`` was relocated into ``.api`` for reuse.
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx

from .api import fetch_tenant_token as get_tenant_token  # re-export for callers

__all__ = [
    "get_tenant_token",
    "download_resource",
    "download_image",
    "download_audio",
    "download_file",
    "transcribe_audio",
    "text_to_speech",
    "cleanup_old_files",
]


async def download_resource(
    client: httpx.AsyncClient,
    token: str,
    message_id: str,
    resource_key: str,
    resource_type: str,
    dest: Path,
) -> Path:
    """Download a message resource (image/file/audio) from Feishu."""
    url = (
        f"https://open.feishu.cn/open-apis/im/v1/messages/"
        f"{message_id}/resources/{resource_key}?type={resource_type}"
    )
    resp = await client.get(url, headers={"Authorization": f"Bearer {token}"})
    resp.raise_for_status()

    # Fix extension for images based on content-type
    if resource_type == "image":
        ct = resp.headers.get("content-type", "")
        if "png" in ct:
            dest = dest.with_suffix(".png")
        elif "gif" in ct:
            dest = dest.with_suffix(".gif")

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(resp.content)
    return dest


async def download_image(
    client: httpx.AsyncClient, token: str, message_id: str, image_key: str, temp_dir: Path
) -> Path:
    dest = temp_dir / f"feishu-img-{int(time.time() * 1000)}.jpg"
    return await download_resource(client, token, message_id, image_key, "image", dest)


async def download_audio(
    client: httpx.AsyncClient, token: str, message_id: str, file_key: str, temp_dir: Path
) -> Path:
    dest = temp_dir / f"feishu-audio-{int(time.time() * 1000)}.opus"
    return await download_resource(client, token, message_id, file_key, "file", dest)


async def download_file(
    client: httpx.AsyncClient,
    token: str,
    message_id: str,
    file_key: str,
    file_name: str,
    temp_dir: Path,
) -> Path:
    dest = temp_dir / (file_name or f"feishu-file-{int(time.time() * 1000)}")
    return await download_resource(client, token, message_id, file_key, "file", dest)


# ── ElevenLabs Audio ─────────────────────────────────────────────


async def transcribe_audio(client: httpx.AsyncClient, api_key: str, audio_path: Path) -> str:
    """Transcribe audio to text via ElevenLabs Scribe API."""
    suffix = audio_path.suffix.lower()
    mime = {
        ".opus": "audio/ogg",
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
    }.get(suffix, "audio/mpeg")

    resp = await client.post(
        "https://api.elevenlabs.io/v1/speech-to-text",
        headers={"xi-api-key": api_key},
        files={"file": (audio_path.name, audio_path.read_bytes(), mime)},
        data={"model_id": "scribe_v1"},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json().get("text", "")


async def text_to_speech(
    client: httpx.AsyncClient, api_key: str, voice_id: str, text: str, temp_dir: Path
) -> Path:
    """Convert text to speech via ElevenLabs TTS. Returns path to opus file."""
    text = text[:2000]  # ElevenLabs limit
    temp_dir.mkdir(parents=True, exist_ok=True)
    opus_path = temp_dir / f"feishu-tts-{int(time.time() * 1000)}.opus"

    resp = await client.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={"xi-api-key": api_key},
        json={
            "text": text,
            "model_id": "eleven_multilingual_v2",
            "output_format": "opus_48000_32",
        },
        timeout=60,
    )
    resp.raise_for_status()
    opus_path.write_bytes(resp.content)
    return opus_path


def cleanup_old_files(temp_dir: Path, max_age_hours: int) -> int:
    """Delete files older than ``max_age_hours`` (recursive). Returns count."""
    if not temp_dir.exists():
        return 0
    cutoff = time.time() - max_age_hours * 3600
    count = 0
    for f in temp_dir.rglob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink()
            count += 1
    return count
