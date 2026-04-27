"""Tests for the simple single-window debounce in NotificationPipeline.

The earlier adaptive short/long behavior was removed (real users typed in
~1-2s spurts that hit the threshold edge constantly). The new contract is:
hold messages for ``debounce_seconds`` of silence per chat, then flush.
"""

import asyncio
import time
import unittest

from xiaobai.core.notifications import NotificationPipeline


class SingleWindowDebounceTests(unittest.IsolatedAsyncioTestCase):
    async def test_single_message_flushes_after_window(self):
        writes = []

        async def write(content, meta):
            writes.append((content, meta, time.perf_counter()))

        t0 = time.perf_counter()
        pipeline = NotificationPipeline(write, debounce_seconds=0.1)
        await pipeline.send("only", {"chat_id": "c1"})

        # Should NOT flush before the window is up.
        await asyncio.sleep(0.05)
        self.assertEqual(len(writes), 0, "must wait the full debounce window")

        # After window passes, flushes.
        await asyncio.sleep(0.1)
        self.assertEqual(len(writes), 1, "single message must flush after silence")
        flush_at = writes[0][2] - t0
        self.assertGreaterEqual(flush_at, 0.1, "must wait at least debounce_seconds")
        self.assertLess(flush_at, 0.3, f"unexpectedly slow flush: {flush_at*1000:.0f}ms")

    async def test_burst_batches_until_silence(self):
        writes = []

        async def write(content, meta):
            writes.append((content, dict(meta)))

        pipeline = NotificationPipeline(write, debounce_seconds=0.1)
        # Three messages spaced inside the window each — keeps resetting timer.
        await pipeline.send("a", {"chat_id": "c1"})
        await asyncio.sleep(0.05)
        await pipeline.send("b", {"chat_id": "c1"})
        await asyncio.sleep(0.05)
        await pipeline.send("c", {"chat_id": "c1"})

        # Each send reset the window — nothing flushed yet.
        self.assertEqual(len(writes), 0)

        # Once silence accumulates, the three flush as one batch.
        await asyncio.sleep(0.2)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][1]["message_type"], "batch")

    async def test_per_chat_isolation(self):
        writes = []

        async def write(content, meta):
            writes.append((content, dict(meta)))

        pipeline = NotificationPipeline(write, debounce_seconds=0.1)
        # Two different chats — each has its own window.
        await pipeline.send("x", {"chat_id": "c1"})
        await pipeline.send("y", {"chat_id": "c2"})

        await asyncio.sleep(0.2)
        self.assertEqual(len(writes), 2)
        chat_ids = sorted(w[1]["chat_id"] for w in writes)
        self.assertEqual(chat_ids, ["c1", "c2"])


if __name__ == "__main__":
    unittest.main()
