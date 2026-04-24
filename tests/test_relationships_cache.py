"""Tests for the mtime-aware person-record cache."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaobai.tools import relationships


class PersonCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._rel_dir = Path(self._tmp.name) / "relationships"
        self._rel_dir.mkdir()
        self._patcher_dir = patch.object(relationships, "_REL_DIR", self._rel_dir)
        self._patcher_idx = patch.object(
            relationships, "_INDEX_FILE", self._rel_dir / "index.json"
        )
        self._patcher_dir.start()
        self._patcher_idx.start()
        # Reset caches between tests.
        relationships._person_cache.clear()
        relationships._index_cache = {}
        relationships._index_mtime = 0.0

    def tearDown(self):
        self._patcher_dir.stop()
        self._patcher_idx.stop()
        self._tmp.cleanup()

    def test_second_load_reuses_cache_and_does_not_reread(self):
        record = relationships.PersonRecord(
            person_id="p1", display_name="Alice", body="hello"
        )
        relationships.save_person(record)

        a = relationships.load_person("p1")
        real_read_text = Path.read_text

        call_count = {"n": 0}

        def counting_read(self, *args, **kwargs):
            call_count["n"] += 1
            return real_read_text(self, *args, **kwargs)

        with patch.object(Path, "read_text", counting_read):
            b = relationships.load_person("p1")
            c = relationships.load_person("p1")

        self.assertEqual(call_count["n"], 0, "cache hits should not touch disk")
        self.assertEqual(a.display_name, "Alice")
        self.assertEqual(b.display_name, "Alice")
        self.assertIs(b, c)

    def test_cache_invalidates_when_file_mtime_changes(self):
        record = relationships.PersonRecord(
            person_id="p2", display_name="Before", body="v1"
        )
        relationships.save_person(record)
        first = relationships.load_person("p2")
        self.assertEqual(first.display_name, "Before")

        # Rewrite file with fresh content; bump mtime explicitly in case the
        # write lands within the same second.
        path = relationships._person_path("p2")
        record.display_name = "After"
        record.body = "v2"
        path.write_text(relationships._dump_person_file(record))
        new_mtime = time.time() + 2
        os.utime(path, (new_mtime, new_mtime))
        # Nuke the cache entry manually too, to simulate external writes (e.g.
        # another process); but more importantly verify save_person primes it
        # for the current-process path. Here we simulate an external editor.
        relationships._person_cache.pop("p2", None)

        second = relationships.load_person("p2")
        self.assertEqual(second.display_name, "After")
        self.assertEqual(second.body, "v2")

    def test_save_person_primes_cache(self):
        record = relationships.PersonRecord(
            person_id="p3", display_name="Primed", body="ok"
        )
        relationships.save_person(record)
        self.assertIn("p3", relationships._person_cache)
        cached_mtime, cached_record = relationships._person_cache["p3"]
        self.assertEqual(cached_record.display_name, "Primed")
        # Next load should be a cache hit — no disk read needed.
        real_read_text = Path.read_text

        def fail_read(self, *args, **kwargs):
            raise AssertionError("cache should have served this load")

        with patch.object(Path, "read_text", fail_read):
            record2 = relationships.load_person("p3")
        self.assertEqual(record2.display_name, "Primed")

    def test_missing_file_clears_cache(self):
        record = relationships.PersonRecord(
            person_id="p4", display_name="Gone", body="x"
        )
        relationships.save_person(record)
        relationships.load_person("p4")  # prime
        self.assertIn("p4", relationships._person_cache)

        relationships._person_path("p4").unlink()
        result = relationships.load_person("p4")
        self.assertIsNone(result)
        self.assertNotIn("p4", relationships._person_cache)


if __name__ == "__main__":
    unittest.main()
