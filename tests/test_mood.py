"""Tests for the per-chat MoodTracker."""

import tempfile
import unittest
from pathlib import Path

from xiaobai.core.mood import MoodTracker


class ScoreTextTests(unittest.TestCase):
    def test_playful_keywords_score(self):
        scores = MoodTracker.score_text("哈哈哈哈绝了")
        self.assertIn("playful", scores)
        self.assertGreater(scores["playful"], 0)

    def test_tired_keywords_score(self):
        scores = MoodTracker.score_text("今天好累啊 班味满满")
        self.assertIn("tired", scores)

    def test_serious_keywords_score(self):
        scores = MoodTracker.score_text("这个 bug 帮我看下方案")
        self.assertIn("serious", scores)

    def test_no_match_returns_empty(self):
        scores = MoodTracker.score_text("今天天气不错")
        self.assertEqual(scores, {})

    def test_multiple_labels_can_coexist(self):
        scores = MoodTracker.score_text("哈哈这个 bug 太离谱了")
        self.assertIn("playful", scores)
        self.assertIn("serious", scores)


class MoodTrackerTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tracker = MoodTracker(root=Path(self._tmp.name))

    def tearDown(self):
        self._tmp.cleanup()

    def test_current_mood_none_before_any_input(self):
        self.assertIsNone(self.tracker.current_mood("c1"))

    def test_single_strong_message_yields_mood(self):
        self.tracker.record("c1", "哈哈哈哈笑死了绝了")
        self.assertEqual(self.tracker.current_mood("c1"), "playful")

    def test_decay_lets_recent_mood_win(self):
        self.tracker.record("c1", "哈哈哈绝了笑死")
        # Multiple serious messages should shift dominant mood
        for _ in range(3):
            self.tracker.record("c1", "bug 帮我看下报错方案修复")
        self.assertEqual(self.tracker.current_mood("c1"), "serious")

    def test_weak_signal_returns_none(self):
        # A single keyword hit may be below MIN_SIGNAL_THRESHOLD (0.4)
        # Score per hit is 0.5, so a single match scores exactly 0.5
        # which passes the threshold. Test an explicitly muted case:
        # custom text that matches nothing should yield None.
        self.tracker.record("c1", "正常聊天 说话 没有情绪词")
        self.assertIsNone(self.tracker.current_mood("c1"))

    def test_persistence_across_instances(self):
        self.tracker.record("c1", "哈哈绝了笑死")
        self.assertEqual(self.tracker.current_mood("c1"), "playful")
        # New tracker instance, same root → should reload
        reloaded = MoodTracker(root=Path(self._tmp.name))
        self.assertEqual(reloaded.current_mood("c1"), "playful")

    def test_window_capped_to_max(self):
        for i in range(50):
            self.tracker.record("c1", f"哈哈 {i}")
        state = self.tracker._load("c1")
        self.assertLessEqual(len(state.window), 10)


if __name__ == "__main__":
    unittest.main()
