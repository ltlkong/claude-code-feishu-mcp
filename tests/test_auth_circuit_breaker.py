"""Circuit-breaker behavior in TokenProvider."""

import asyncio
import unittest

from xiaobai.core.auth import TokenFetchUnavailable, TokenProvider


class CircuitBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_raises_when_fetch_fails(self):
        calls = {"n": 0}

        async def fetch(_http):
            calls["n"] += 1
            raise RuntimeError("auth down")

        tp = TokenProvider(
            name="t", fetch=fetch, http=None, ttl_seconds=60, failure_cooldown_seconds=1.0
        )
        with self.assertRaises(RuntimeError):
            await tp.get()
        self.assertEqual(calls["n"], 1)

    async def test_circuit_opens_after_failure_until_cooldown(self):
        calls = {"n": 0}

        async def fetch(_http):
            calls["n"] += 1
            raise RuntimeError("auth down")

        tp = TokenProvider(
            name="t",
            fetch=fetch,
            http=None,
            ttl_seconds=60,
            failure_cooldown_seconds=10.0,
        )
        with self.assertRaises(RuntimeError):
            await tp.get()
        # Next N callers should fail fast without a second fetch attempt.
        for _ in range(5):
            with self.assertRaises(TokenFetchUnavailable):
                await tp.get()
        self.assertEqual(calls["n"], 1, "only the first attempt should hit fetch")

    async def test_concurrent_callers_during_outage_share_the_one_attempt(self):
        calls = {"n": 0}

        async def fetch(_http):
            calls["n"] += 1
            await asyncio.sleep(0.02)
            raise RuntimeError("auth down")

        tp = TokenProvider(
            name="t",
            fetch=fetch,
            http=None,
            ttl_seconds=60,
            failure_cooldown_seconds=10.0,
        )

        async def call():
            try:
                await tp.get()
            except Exception as e:
                return type(e).__name__

        results = await asyncio.gather(*(call() for _ in range(10)))
        self.assertEqual(calls["n"], 1, "single-flight + cooldown limits to one fetch")
        # One of them saw RuntimeError, the rest saw the fast-fail.
        self.assertEqual(results.count("RuntimeError"), 1)
        self.assertEqual(results.count("TokenFetchUnavailable"), 9)

    async def test_successful_fetch_clears_failure_state(self):
        call_log = []

        async def fetch(_http):
            # First call fails, second succeeds.
            call_log.append("hit")
            if len(call_log) == 1:
                raise RuntimeError("transient")
            return "token-value"

        tp = TokenProvider(
            name="t",
            fetch=fetch,
            http=None,
            ttl_seconds=60,
            failure_cooldown_seconds=0.0,
        )
        with self.assertRaises(RuntimeError):
            await tp.get()
        # cooldown=0 → next call should try again and succeed.
        value = await tp.get()
        self.assertEqual(value, "token-value")
        # Further calls are cached hits.
        value2 = await tp.get()
        self.assertEqual(value2, "token-value")
        self.assertEqual(len(call_log), 2)


if __name__ == "__main__":
    unittest.main()
