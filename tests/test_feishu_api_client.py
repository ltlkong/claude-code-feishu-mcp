"""Tests for FeishuApiClient retry-on-token-error semantics."""

import unittest
from unittest.mock import AsyncMock, MagicMock

from xiaobai.channels.feishu.api_client import FeishuApiClient


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


class FeishuApiClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_happy_path_passes_through_body(self):
        http = MagicMock()
        http.request = AsyncMock(return_value=_FakeResponse({"code": 0, "data": {"ok": True}}))
        token = MagicMock()
        token.get = AsyncMock(return_value="tok-1")
        token.invalidate = MagicMock()

        client = FeishuApiClient(http, token)
        data = await client.post_json("https://x/y", {"k": "v"})

        self.assertEqual(data, {"code": 0, "data": {"ok": True}})
        http.request.assert_awaited_once()
        args, kwargs = http.request.await_args
        self.assertEqual(args, ("POST", "https://x/y"))
        self.assertEqual(kwargs["json"], {"k": "v"})
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok-1")
        token.invalidate.assert_not_called()

    async def test_retries_on_token_error_code(self):
        # First response signals token expiry; second succeeds.
        responses = iter([
            _FakeResponse({"code": 99991663, "msg": "token expired"}),
            _FakeResponse({"code": 0, "data": {"ok": True}}),
        ])
        http = MagicMock()
        http.request = AsyncMock(side_effect=lambda *a, **k: next(responses))
        token = MagicMock()
        token.get = AsyncMock(side_effect=["tok-1", "tok-2"])
        token.invalidate = MagicMock()

        client = FeishuApiClient(http, token)
        data = await client.post_json("https://x/y", {"k": "v"})

        self.assertEqual(data["code"], 0)
        self.assertEqual(http.request.await_count, 2)
        self.assertEqual(token.get.await_count, 2)
        token.invalidate.assert_called_once()

    async def test_only_one_retry_on_token_error(self):
        # Two consecutive token-error responses — second one should be returned.
        responses = iter([
            _FakeResponse({"code": 99991663, "msg": "expired"}),
            _FakeResponse({"code": 99991663, "msg": "still expired"}),
        ])
        http = MagicMock()
        http.request = AsyncMock(side_effect=lambda *a, **k: next(responses))
        token = MagicMock()
        token.get = AsyncMock(return_value="tok")
        token.invalidate = MagicMock()

        client = FeishuApiClient(http, token)
        data = await client.post_json("https://x/y", {"k": "v"})

        self.assertEqual(data["code"], 99991663)
        self.assertEqual(http.request.await_count, 2)
        token.invalidate.assert_called_once()

    async def test_domain_errors_are_returned_not_retried(self):
        http = MagicMock()
        http.request = AsyncMock(return_value=_FakeResponse({"code": 230002, "msg": "not member"}))
        token = MagicMock()
        token.get = AsyncMock(return_value="tok")
        token.invalidate = MagicMock()

        client = FeishuApiClient(http, token)
        data = await client.post_json("https://x/y", {"k": "v"})

        self.assertEqual(data["code"], 230002)
        http.request.assert_awaited_once()
        token.invalidate.assert_not_called()


if __name__ == "__main__":
    unittest.main()
