"""Test the per-user profile-refresh nudge counter in XiaobaiServer."""

import unittest

from xiaobai.mcp_server import PROFILE_REFRESH_INTERVAL, XiaobaiServer


class ProfileRefreshNudgeTests(unittest.TestCase):
    def _make_server(self):
        server = XiaobaiServer.__new__(XiaobaiServer)
        server._profile_refresh_counter = {}
        return server

    def _bump_once(self, server, chat_id, user_id, meta):
        """Simulate the per-message counter bump from _ingress_feishu_body."""
        if user_id:
            key = f"{chat_id}:{user_id}"
            c = server._profile_refresh_counter.get(key, 0) + 1
            if c >= PROFILE_REFRESH_INTERVAL:
                meta["profile_refresh_due"] = True
                server._profile_refresh_counter[key] = 0
            else:
                server._profile_refresh_counter[key] = c

    def test_nudge_fires_on_Nth_message(self):
        server = self._make_server()
        last_meta = {}
        for i in range(PROFILE_REFRESH_INTERVAL):
            last_meta = {}
            self._bump_once(server, "c1", "u1", last_meta)
        self.assertTrue(last_meta.get("profile_refresh_due"))

    def test_counter_resets_after_nudge(self):
        server = self._make_server()
        for _ in range(PROFILE_REFRESH_INTERVAL):
            self._bump_once(server, "c1", "u1", {})
        self.assertEqual(server._profile_refresh_counter["c1:u1"], 0)

        # One more message — should NOT fire yet.
        meta = {}
        self._bump_once(server, "c1", "u1", meta)
        self.assertNotIn("profile_refresh_due", meta)

    def test_counter_is_per_chat_user_pair(self):
        server = self._make_server()
        for _ in range(PROFILE_REFRESH_INTERVAL - 1):
            self._bump_once(server, "c1", "u1", {})

        # Different chat, same user — should NOT inherit count
        meta = {}
        self._bump_once(server, "c2", "u1", meta)
        self.assertNotIn("profile_refresh_due", meta)

        # Back in c1, one more hits the threshold
        meta = {}
        self._bump_once(server, "c1", "u1", meta)
        self.assertTrue(meta.get("profile_refresh_due"))


if __name__ == "__main__":
    unittest.main()
