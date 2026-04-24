"""Tests for providers.routing.select_model."""

import unittest

from xiaobai.providers.base import ProviderEvent
from xiaobai.providers.routing import select_model


def _event(content: str = "hi", **meta) -> ProviderEvent:
    return ProviderEvent(content=content, meta=meta)


class SelectModelTests(unittest.TestCase):
    def test_heartbeat_goes_cheap(self):
        decision = select_model(_event(message_type="heartbeat"))
        self.assertEqual(decision.tier, "cheap")

    def test_reaction_goes_cheap(self):
        decision = select_model(_event(message_type="reaction"))
        self.assertEqual(decision.tier, "cheap")

    def test_override_wins_over_heuristic(self):
        decision = select_model(_event(message_type="heartbeat", _tier="expensive"))
        self.assertEqual(decision.tier, "expensive")
        self.assertEqual(decision.reason, "override")

    def test_large_content_stays_expensive(self):
        decision = select_model(_event(content="x" * 20_000, message_type="text"))
        self.assertEqual(decision.tier, "expensive")
        self.assertIn("large_content", decision.reason)

    def test_default_text_is_expensive(self):
        decision = select_model(_event(content="老板好", message_type="text"))
        self.assertEqual(decision.tier, "expensive")
        self.assertEqual(decision.reason, "default")

    def test_empty_meta_is_expensive(self):
        decision = select_model(ProviderEvent(content="hi", meta={}))
        self.assertEqual(decision.tier, "expensive")


if __name__ == "__main__":
    unittest.main()
