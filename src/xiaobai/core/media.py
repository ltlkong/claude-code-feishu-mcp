"""Short-id / path registry for media files shared across channels.

This is a placeholder for Session 2's tool layer — it replaces the ad-hoc
``_media_hashes`` map in ``feishu_channel/server.py`` (lines 428, 2358–2373)
with a generic interface. Session 1 does not wire it in; the existing
server.py keeps using its own map.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path


class MediaRegistry:
    """Track recently downloaded media by MD5 for duplicate detection.

    Also provides a simple short_id → path map so tool handlers can refer to
    media by opaque short IDs instead of absolute paths (Session 2).
    """

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._ttl = ttl_seconds
        # md5 -> (path, sender_alias, timestamp)
        self._hashes: dict[str, tuple[str, str, float]] = {}
        # short_id -> path
        self._paths: dict[str, Path] = {}
        self._counter = 0

    # ── Dedup ────────────────────────────────────────────────────

    def check_dup(self, path: Path, sender: str = "") -> str | None:
        """Compute md5 of ``path`` and return a duplicate note or None.

        Side effect: records ``path`` under its md5 for future checks.
        """
        try:
            md5 = hashlib.md5(path.read_bytes()).hexdigest()
        except Exception:
            return None
        now = time.time()
        self._evict_stale(now)
        if md5 in self._hashes:
            orig_path, orig_sender, _ = self._hashes[md5]
            return f" (duplicate of {orig_sender}'s earlier media: {orig_path})"
        self._hashes[md5] = (str(path), sender, now)
        return None

    def _evict_stale(self, now: float) -> None:
        stale = [h for h, (_, _, t) in self._hashes.items() if now - t > self._ttl]
        for h in stale:
            del self._hashes[h]

    # ── Short IDs ────────────────────────────────────────────────

    def register_path(self, path: Path) -> str:
        """Register a path and return an opaque short id (e.g. ``m17``)."""
        self._counter += 1
        sid = f"m{self._counter}"
        self._paths[sid] = path
        return sid

    def resolve(self, short_or_full: str) -> Path | None:
        """Return the path for a short id, or None if unknown."""
        return self._paths.get(short_or_full)

    def cleanup_old_files(self, temp_dir: Path, max_age_hours: int) -> int:
        """Delete files older than ``max_age_hours`` under ``temp_dir``.

        Mirrors ``feishu_channel.media.cleanup_old_files`` for consistency.
        """
        if not temp_dir.exists():
            return 0
        cutoff = time.time() - max_age_hours * 3600
        count = 0
        for f in temp_dir.rglob("*"):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                count += 1
        return count
