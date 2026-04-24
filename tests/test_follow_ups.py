"""Tests for core.follow_ups + tools.follow_ups.manage_follow_up."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from xiaobai.core import follow_ups as core_fu
from xiaobai.tools import follow_ups as tool_fu


class ParseDueTests(unittest.TestCase):
    def test_relative_days(self):
        now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
        parsed = core_fu.parse_due("+3d", now=now)
        self.assertEqual(parsed, datetime(2026, 4, 27, 10, 0, tzinfo=timezone.utc))

    def test_relative_hours(self):
        now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
        parsed = core_fu.parse_due("48h", now=now)
        self.assertEqual(parsed, datetime(2026, 4, 26, 10, 0, tzinfo=timezone.utc))

    def test_relative_weeks(self):
        now = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
        parsed = core_fu.parse_due("+2w", now=now)
        self.assertEqual(parsed, datetime(2026, 5, 8, 10, 0, tzinfo=timezone.utc))

    def test_iso_utc_z(self):
        parsed = core_fu.parse_due("2026-05-01T09:00:00Z")
        self.assertEqual(parsed, datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc))

    def test_iso_with_offset_converts_to_utc(self):
        parsed = core_fu.parse_due("2026-05-01T17:00:00+08:00")
        self.assertEqual(parsed, datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc))

    def test_bare_date(self):
        parsed = core_fu.parse_due("2026-05-01")
        self.assertEqual(parsed, datetime(2026, 5, 1, 0, 0, tzinfo=timezone.utc))

    def test_garbage_returns_none(self):
        self.assertIsNone(core_fu.parse_due("not a date"))
        self.assertIsNone(core_fu.parse_due(""))


class StorageRoundtripTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_save_then_load(self):
        fu = core_fu.FollowUp(
            id="fu_abc",
            chat_id="c1",
            person_id="p1",
            topic="vacation",
            context="went to Thailand",
            due_at="2026-05-01T09:00:00Z",
            created_at="2026-04-24T00:00:00Z",
        )
        core_fu.save(fu, root=self.root)
        loaded = core_fu.load("fu_abc", root=self.root)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.topic, "vacation")
        self.assertEqual(loaded.status, "pending")

    def test_list_all_filters_by_chat_and_status(self):
        for i, chat in enumerate(["c1", "c2", "c1"]):
            core_fu.save(
                core_fu.FollowUp(
                    id=f"fu_{i}",
                    chat_id=chat,
                    person_id="p1",
                    topic=f"t{i}",
                    context="",
                    due_at="2026-05-01T09:00:00Z",
                    created_at="2026-04-24T00:00:00Z",
                ),
                root=self.root,
            )
        c1 = core_fu.list_all(chat_id="c1", root=self.root)
        self.assertEqual(len(c1), 2)
        c2 = core_fu.list_all(chat_id="c2", root=self.root)
        self.assertEqual(len(c2), 1)


class ManageFollowUpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patcher = patch.object(core_fu, "_STORE_DIR", Path(self._tmp.name))
        self._patcher.start()

        # Stub out reminders_cli so tests don't touch crontab.
        self._reminder_patch_create = patch.object(
            tool_fu, "_create_reminder", return_value={"status": "ok"}
        )
        self._reminder_patch_delete = patch.object(
            tool_fu, "_delete_reminder", return_value={"status": "ok"}
        )
        self._create_mock = self._reminder_patch_create.start()
        self._delete_mock = self._reminder_patch_delete.start()

    def tearDown(self):
        self._patcher.stop()
        self._reminder_patch_create.stop()
        self._reminder_patch_delete.stop()
        self._tmp.cleanup()

    def test_add_creates_follow_up_and_reminder(self):
        result = tool_fu.manage_follow_up(
            "add",
            chat_id="c1",
            person_id="p1",
            topic="vacation check",
            context="going to Thailand until 5/3",
            due_at="+7d",
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["follow_up_id"].startswith("fu_"))
        self._create_mock.assert_called_once()
        # smart reminder
        call_kwargs = self._create_mock.call_args.kwargs
        self.assertTrue(call_kwargs["smart"])
        self.assertEqual(call_kwargs["max_runs"], 1)

    def test_add_rejects_past_due(self):
        result = tool_fu.manage_follow_up(
            "add",
            chat_id="c1",
            topic="t",
            due_at="2020-01-01T00:00:00Z",
        )
        self.assertEqual(result["status"], "error")
        self.assertIn("future", result["message"])

    def test_add_requires_core_fields(self):
        missing_chat = tool_fu.manage_follow_up("add", topic="t", due_at="+1d")
        self.assertEqual(missing_chat["status"], "error")

        missing_topic = tool_fu.manage_follow_up("add", chat_id="c1", due_at="+1d")
        self.assertEqual(missing_topic["status"], "error")

        missing_due = tool_fu.manage_follow_up("add", chat_id="c1", topic="t")
        self.assertEqual(missing_due["status"], "error")

    def test_list_returns_pending(self):
        tool_fu.manage_follow_up(
            "add", chat_id="c1", topic="a", due_at="+1d"
        )
        tool_fu.manage_follow_up(
            "add", chat_id="c2", topic="b", due_at="+2d"
        )
        all_items = tool_fu.manage_follow_up("list")
        self.assertEqual(all_items["count"], 2)
        only_c1 = tool_fu.manage_follow_up("list", chat_id="c1")
        self.assertEqual(only_c1["count"], 1)

    def test_complete_marks_status_and_deletes_reminder(self):
        added = tool_fu.manage_follow_up(
            "add", chat_id="c1", topic="vacation", due_at="+3d"
        )
        fu_id = added["follow_up_id"]
        closed = tool_fu.manage_follow_up(
            "complete", follow_up_id=fu_id, note="they had fun"
        )
        self.assertEqual(closed["status"], "ok")
        self.assertEqual(closed["new_status"], "completed")
        self._delete_mock.assert_called_once()

        # Should no longer show up in the pending list
        listed = tool_fu.manage_follow_up("list")
        self.assertEqual(listed["count"], 0)

    def test_unknown_action(self):
        result = tool_fu.manage_follow_up("delete")
        self.assertEqual(result["status"], "error")


if __name__ == "__main__":
    unittest.main()
