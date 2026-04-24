"""Tests for adaptive heartbeat: backoff, reset, quiet hours, mute list."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaobai.tools import heartbeat as hb


class AdaptiveHeartbeatTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._wl = Path(self._tmp.name) / "watchlist.json"
        self._mute = Path(self._tmp.name) / "mute.json"
        self._patchers = [
            patch.object(hb, "_WATCHLIST_FILE", self._wl),
            patch.object(hb, "_MUTE_FILE", self._mute),
        ]
        for p in self._patchers:
            p.start()
        # Clear per-process state between tests.
        hb._last_activity.clear()
        hb._last_bot_reply.clear()
        hb._msg_counts.clear()
        hb._pending_auto_labels.clear()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def test_effective_interval_grows_with_silent_ticks(self):
        info = {"interval": 15, "silent_ticks": 0}
        self.assertEqual(hb._effective_interval(info), 15)
        info["silent_ticks"] = 3
        self.assertEqual(hb._effective_interval(info), 15 + 3 * hb.SILENT_GROWTH_MINUTES)

    def test_effective_interval_capped_at_max(self):
        info = {"interval": 15, "silent_ticks": 100}
        self.assertEqual(hb._effective_interval(info), hb.MAX_INTERVAL_MINUTES)


class MuteListTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._wl = Path(self._tmp.name) / "watchlist.json"
        self._mute = Path(self._tmp.name) / "mute.json"
        self._patchers = [
            patch.object(hb, "_WATCHLIST_FILE", self._wl),
            patch.object(hb, "_MUTE_FILE", self._mute),
        ]
        for p in self._patchers:
            p.start()
        hb._msg_counts.clear()
        hb._pending_auto_labels.clear()

    def tearDown(self):
        for p in self._patchers:
            p.stop()
        self._tmp.cleanup()

    def test_remove_mutes_and_prevents_auto_readd(self):
        # Seed a watchlist entry and pretend it was auto-added.
        hb.manage_heartbeat("add", chat_id="c1")
        result = hb.manage_heartbeat("remove", chat_id="c1")
        self.assertEqual(result["status"], "ok")
        self.assertIn("c1", hb._load_mute())

        # Simulate bursts of incoming messages — auto-add should skip.
        for _ in range(20):
            hb.mark_activity("c1", label="c1")
        hb._flush_auto_adds()
        wl = hb._load_watchlist()
        self.assertNotIn("c1", wl, "muted chat must not auto-re-add")

    def test_explicit_add_unmutes(self):
        hb.manage_heartbeat("add", chat_id="c2")
        hb.manage_heartbeat("remove", chat_id="c2")
        self.assertIn("c2", hb._load_mute())

        # Explicit re-add clears the mute.
        hb.manage_heartbeat("add", chat_id="c2")
        self.assertNotIn("c2", hb._load_mute())
        self.assertIn("c2", hb._load_watchlist())

    def test_remove_of_never_added_still_mutes(self):
        result = hb.manage_heartbeat("remove", chat_id="c3")
        self.assertEqual(result["status"], "ok")
        self.assertIn("c3", hb._load_mute())


class BotReplyResetTests(unittest.TestCase):
    def test_mark_bot_reply_records_timestamp(self):
        hb._last_bot_reply.clear()
        hb.mark_bot_reply("c1")
        self.assertGreater(hb._last_bot_reply.get("c1", 0), 0)

    def test_mark_bot_reply_empty_chat_is_noop(self):
        before = dict(hb._last_bot_reply)
        hb.mark_bot_reply("")
        self.assertEqual(hb._last_bot_reply, before)


if __name__ == "__main__":
    unittest.main()
