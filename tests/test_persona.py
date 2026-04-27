"""Tests for core.persona — timezone resolution + hour bucketing."""

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from xiaobai.core.persona import (
    hour_bucket,
    persona_signal,
    resolve_timezone,
)


class ResolveTimezoneTests(unittest.TestCase):
    def test_explicit_iana_wins(self):
        self.assertEqual(resolve_timezone(tz_str="America/Vancouver"), "America/Vancouver")

    def test_bad_iana_falls_through_to_location(self):
        self.assertEqual(
            resolve_timezone(tz_str="NotAZone", location="vancouver"),
            "America/Vancouver",
        )

    def test_chinese_location_resolves(self):
        self.assertEqual(resolve_timezone(location="福州"), "Asia/Shanghai")
        self.assertEqual(resolve_timezone(location="烟台"), "Asia/Shanghai")

    def test_english_location_resolves(self):
        self.assertEqual(resolve_timezone(location="Vancouver"), "America/Vancouver")

    def test_substring_match(self):
        self.assertEqual(
            resolve_timezone(location="人在福州，但下周去莱阳"), "Asia/Shanghai",
        )

    def test_unknown_returns_none(self):
        self.assertIsNone(resolve_timezone(location="Atlantis"))
        self.assertIsNone(resolve_timezone())


class HourBucketTests(unittest.TestCase):
    def test_all_buckets(self):
        self.assertEqual(hour_bucket(3), "deep_night")
        self.assertEqual(hour_bucket(7), "morning")
        self.assertEqual(hour_bucket(14), "day")
        self.assertEqual(hour_bucket(20), "evening")
        self.assertEqual(hour_bucket(23), "late_night")
        self.assertEqual(hour_bucket(1), "late_night")


class PersonaSignalTests(unittest.TestCase):
    def test_signal_respects_timezone(self):
        # Pin "now" to a known UTC moment so the derived local hour is deterministic.
        fixed_utc = datetime(2026, 4, 24, 16, 0, tzinfo=ZoneInfo("UTC"))
        sig = persona_signal(location="Vancouver", now=fixed_utc)
        # Vancouver is UTC-7 (PDT) in April; 16:00 UTC → 9:00 local
        # (cast to str — meta values are stringified for Zod validation)
        self.assertEqual(sig["user_local_hour"], "9")
        self.assertEqual(sig["hour_bucket"], "morning")
        self.assertEqual(sig["user_timezone"], "America/Vancouver")

    def test_signal_for_china_timezone(self):
        fixed_utc = datetime(2026, 4, 24, 18, 0, tzinfo=ZoneInfo("UTC"))
        sig = persona_signal(location="福州", now=fixed_utc)
        # UTC+8 → 02:00 local = deep_night
        self.assertEqual(sig["user_local_hour"], "2")
        self.assertEqual(sig["hour_bucket"], "deep_night")

    def test_unknown_location_returns_empty(self):
        self.assertEqual(persona_signal(location="Atlantis"), {})

    def test_empty_input_returns_empty(self):
        self.assertEqual(persona_signal(), {})


if __name__ == "__main__":
    unittest.main()
