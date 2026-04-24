"""Per-chat mood tracker — rule-based, cheap, continuous.

Each ``record`` call scores a text against a keyword/emoji lexicon and
appends to a sliding window of the last ``MAX_WINDOW`` scored events.
``current_mood`` returns the dominant label after applying a recency
weight (``DECAY`` per step back in time), or ``None`` when the signal
is too weak to distinguish from noise.

Why rules, not a model: a message-level LLM classifier would add 200ms
of latency per message plus API cost for something that's ~95% accurate
from keywords. The remaining 5% (sarcasm, mixed feelings) is better
handled by Claude reading the actual message text than by a noisy mood
label.

The tracker persists to ``workspace/state/mood/{chat_id}.json`` so mood
state survives process restarts — otherwise every restart hard-resets
every chat to "neutral" and ``meta['mood_signal']`` goes blank for the
first few messages.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# Keyword / emoji lexicon. Keep entries STRONG — ambiguous words cause
# false positives and mute Claude's natural reading of the actual text.
_LEXICON: dict[str, list[str]] = {
    "tired": [
        "累", "烦", "无语", "哭死", "疲惫", "乏", "班味", "牛马",
        "怨种", "心累", "哎", "叹气", "😮‍💨", "🥱",
    ],
    "playful": [
        "哈哈", "笑死", "绝了", "离谱", "救命", "xswl",
        "[大笑]", "[呲牙]", "[偷笑]", "[撇嘴]", "🤣", "😂", "😆",
        "属于是", "嘴替", "降维打击", "摆烂", "破防",
    ],
    "sad": [
        "难过", "不开心", "emo", "委屈", "想哭", "失落",
        "😭", "🥺", "😢", "😔",
    ],
    "serious": [
        "帮我看", "报错", "方案", "数据", "bug", "修复", "review",
        "架构", "性能", "优化", "commit", "pr", "merge",
        "怎么办", "怎么弄",
    ],
    "urgent": [
        "急", "立刻", "赶紧", "快点", "马上", "！！", "!!", "？？", "??",
    ],
}

MAX_WINDOW = 10
DECAY = 0.7
MIN_SIGNAL_THRESHOLD = 0.4

_DEFAULT_MOOD_DIR = (
    Path(__file__).resolve().parents[2]
    / ".." / "workspace" / "state" / "mood"
).resolve()


@dataclass
class _Record:
    timestamp: float
    scores: dict[str, float]


@dataclass
class _ChatState:
    window: list[_Record] = field(default_factory=list)


class MoodTracker:
    """Rolling rule-based mood classifier with on-disk persistence."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = root or _DEFAULT_MOOD_DIR
        self._states: dict[str, _ChatState] = {}

    # ── Public API ──────────────────────────────────────────────

    @staticmethod
    def score_text(text: str) -> dict[str, float]:
        """Return a sparse ``{label: score}`` for ``text``.

        Score counts keyword hits (capped at 1.0 per label). Empty
        text or no matches → empty dict.
        """
        if not text:
            return {}
        lower = text.lower()
        scores: dict[str, float] = {}
        for label, keywords in _LEXICON.items():
            hits = 0
            for kw in keywords:
                hits += lower.count(kw.lower())
            if hits:
                scores[label] = min(1.0, hits * 0.5)
        return scores

    def record(
        self, chat_id: str, text: str, timestamp: float | None = None
    ) -> None:
        """Score and append ``text`` to ``chat_id``'s window. No-op if no signal."""
        if not chat_id or not text:
            return
        scores = self.score_text(text)
        if not scores:
            return
        state = self._load(chat_id)
        state.window.append(_Record(timestamp or time.time(), scores))
        if len(state.window) > MAX_WINDOW:
            state.window = state.window[-MAX_WINDOW:]
        self._save(chat_id)

    def current_mood(self, chat_id: str) -> str | None:
        """Return the dominant mood label, or ``None`` if signal is too weak."""
        state = self._load(chat_id)
        if not state.window:
            return None
        aggregate: dict[str, float] = {}
        n = len(state.window)
        for idx, rec in enumerate(state.window):
            weight = DECAY ** (n - 1 - idx)
            for label, s in rec.scores.items():
                aggregate[label] = aggregate.get(label, 0.0) + s * weight
        if not aggregate:
            return None
        label, score = max(aggregate.items(), key=lambda kv: kv[1])
        if score < MIN_SIGNAL_THRESHOLD:
            return None
        return label

    # ── Persistence ─────────────────────────────────────────────

    def _path(self, chat_id: str) -> Path:
        safe = chat_id.replace("/", "_").replace("\\", "_").replace(":", "_")
        return self._root / f"{safe}.json"

    def _load(self, chat_id: str) -> _ChatState:
        cached = self._states.get(chat_id)
        if cached is not None:
            return cached
        p = self._path(chat_id)
        state = _ChatState()
        if p.is_file():
            try:
                data = json.loads(p.read_text())
                state.window = [
                    _Record(float(r["timestamp"]), dict(r["scores"]))
                    for r in data.get("window", [])
                    if isinstance(r, dict)
                ]
            except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as e:
                logger.debug("mood load failed for %s: %s", chat_id, e)
        self._states[chat_id] = state
        return state

    def _save(self, chat_id: str) -> None:
        state = self._states.get(chat_id)
        if not state:
            return
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            payload = {"window": [asdict(r) for r in state.window]}
            self._path(chat_id).write_text(
                json.dumps(payload, ensure_ascii=False)
            )
        except OSError as e:
            logger.debug("mood save failed for %s: %s", chat_id, e)
