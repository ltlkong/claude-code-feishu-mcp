"""Tests for the adaptive short/long debounce in NotificationPipeline."""

import asyncio
import time
import unittest

from xiaobai.core.notifications import NotificationPipeline


class AdaptiveDebounceTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_message_uses_short_window(self):
        writes = []

        async def write(content, meta):
            writes.append((content, meta, time.perf_counter()))

        t0 = time.perf_counter()
        pipeline = NotificationPipeline(
            write,
            short_debounce_seconds=0.05,
            long_debounce_seconds=0.5,
        )
        await pipeline.send("only", {"chat_id": "c1"})

        # Wait just past the short window — the single message should have flushed.
        await asyncio.sleep(0.1)
        self.assertEqual(len(writes), 1, "single message should flush via short window")
        flush_at = writes[0][2] - t0
        self.assertLess(flush_at, 0.2, f"expected <200ms flush, got {flush_at*1000:.0f}ms")

    async def test_burst_uses_long_window(self):
        writes = []

        async def write(content, meta):
            writes.append((content, dict(meta)))

        pipeline = NotificationPipeline(
            write,
            short_debounce_seconds=0.05,
            long_debounce_seconds=0.5,
        )
        # Two messages immediately → burst path (long window kicks in)
        await pipeline.send("a", {"chat_id": "c1"})
        await pipeline.send("b", {"chat_id": "c1"})

        # Wait past short window but before long window — should NOT flush yet.
        await asyncio.sleep(0.15)
        self.assertEqual(len(writes), 0, "burst should wait for long window")

        # Wait past long window — both flush as a batch.
        await asyncio.sleep(0.5)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][1]["message_type"], "batch")

    async def test_legacy_single_debounce_arg_still_works(self):
        writes = []

        async def write(content, meta):
            writes.append((content, meta))

        pipeline = NotificationPipeline(write, debounce_seconds=0.05)
        await pipeline.send("x", {"chat_id": "c1"})
        await asyncio.sleep(0.12)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], "x")


if __name__ == "__main__":
    unittest.main()
