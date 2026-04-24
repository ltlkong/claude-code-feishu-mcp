"""Tests for the channel-agnostic card protocol."""

import unittest
from unittest.mock import AsyncMock

from xiaobai.core.card_protocol import CardService, StatelessCardService
from xiaobai.channels.feishu.cards import CardManager


class ProtocolConformanceTests(unittest.TestCase):
    def test_feishu_card_manager_conforms(self):
        # CardManager is the Feishu implementation — it predates the
        # protocol but exposes the same shape.
        self.assertTrue(hasattr(CardManager, "register_pending"))
        self.assertTrue(hasattr(CardManager, "cancel_pending"))
        self.assertTrue(hasattr(CardManager, "create_card"))
        self.assertTrue(hasattr(CardManager, "update_card"))
        self.assertTrue(hasattr(CardManager, "finalize_card"))
        self.assertTrue(hasattr(CardManager, "cleanup_stale_cards"))

    def test_stateless_service_conforms(self):
        svc = StatelessCardService(send_text=AsyncMock())
        self.assertIsInstance(svc, CardService)


class StatelessCardServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_finalize_sends_the_final_text_once(self):
        sent = []

        async def send_text(chat_id, text):
            sent.append((chat_id, text))
            return {"status": "ok"}

        svc = StatelessCardService(send_text)
        svc.register_pending("r1", "c1", "m1")
        await svc.create_card("r1", "working", "step 1")
        await svc.update_card("r1", "working", "step 2")
        result = await svc.finalize_card("r1", "done.")

        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(sent, [("c1", "done.")])

    async def test_cancel_pending_removes_slot(self):
        svc = StatelessCardService(send_text=AsyncMock())
        svc.register_pending("r2", "c2", "m2")
        svc.cancel_pending("r2")
        result = await svc.finalize_card("r2", "no-op")
        self.assertEqual(result["status"], "error")
        self.assertIn("unknown request_id", result["message"])

    async def test_cleanup_is_noop(self):
        svc = StatelessCardService(send_text=AsyncMock())
        self.assertEqual(await svc.cleanup_stale_cards(), 0)


if __name__ == "__main__":
    unittest.main()
