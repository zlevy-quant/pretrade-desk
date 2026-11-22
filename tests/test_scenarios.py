"""Tests for the scenario walkthrough.

Each scenario must produce the outcome it advertises, so the live demo and the
test suite cannot drift apart.

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest

from pretrade.domain import Decision
from pretrade.scenarios import run_scenario, scenarios


class ScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.by_key = {s.key: s for s in scenarios()}

    def run_key(self, key: str):
        return run_scenario(self.by_key[key])

    def test_keys_are_unique_and_present(self) -> None:
        keys = [s.key for s in scenarios()]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(
            set(keys),
            {"clean_allow", "collar_override", "stale_quote", "kill_switch",
             "duplicate_burst", "restricted_account", "closing_only"},
        )

    def test_clean_allow(self) -> None:
        o = self.run_key("clean_allow")
        self.assertEqual(o.verdict.decision, Decision.ALLOW)
        self.assertEqual(o.final_status, "ACCEPTED")
        self.assertFalse(o.overridden)

    def test_collar_override_is_soft_then_released(self) -> None:
        o = self.run_key("collar_override")
        self.assertEqual(o.verdict.decision, Decision.BLOCK)
        self.assertTrue(o.verdict.overridable)
        self.assertTrue(o.overridden)
        self.assertEqual(o.final_status, "OVERRIDDEN")

    def test_data_and_regulatory_blocks_are_hard(self) -> None:
        for key in ("stale_quote", "kill_switch", "duplicate_burst",
                    "restricted_account", "closing_only"):
            with self.subTest(scenario=key):
                o = self.run_key(key)
                self.assertEqual(o.verdict.decision, Decision.BLOCK)
                self.assertFalse(o.verdict.overridable)   # hard, not releasable
                self.assertFalse(o.overridden)
                self.assertEqual(o.final_status, "BLOCKED")

    def test_every_block_names_a_driving_control(self) -> None:
        for s in scenarios():
            o = run_scenario(s)
            if not o.verdict.allowed:
                self.assertTrue(o.verdict.blocking_controls(),
                                f"{s.key} blocked with no blocking control named")


if __name__ == "__main__":
    unittest.main()
