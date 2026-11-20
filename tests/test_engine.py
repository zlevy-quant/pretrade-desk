"""Engine and control behaviour tests (standard-library unittest).

Run: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import timedelta
from decimal import Decimal

from pretrade.controls import EvaluationContext, default_controls
from pretrade.domain import ControlKind, ControlStatus, Decision, Order, OrderType, Side
from pretrade.engine import PreTradeEngine
from pretrade.market_data import utcnow
from pretrade.store import Store


def order(**kw) -> Order:
    base = dict(account_id="ACME-0042", symbol="AAPL", side=Side.BUY,
                order_type=OrderType.MARKET, quantity=100, limit_price=None, tenant="RIA")
    base.update(kw)
    return Order(**base)


class EngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.store.seed_if_empty()
        self.engine = PreTradeEngine(default_controls())
        self.now = utcnow()

    def ctx(self) -> EvaluationContext:
        return EvaluationContext(store=self.store, config=self.store.get_config(),
                                 market_data=self.store, now=self.now, actor="tester")

    def result(self, verdict, name):
        return next(c for c in verdict.controls if c.name == name)

    # ---- happy path ----
    def test_clean_market_buy_is_allowed(self):
        v = self.engine.evaluate(order(), self.ctx())
        self.assertEqual(v.decision, Decision.ALLOW)
        self.assertFalse(v.overridable)
        self.assertEqual(self.result(v, "field_validation").status, ControlStatus.PASS)

    # ---- field validation (hard) ----
    def test_non_positive_quantity_blocks_hard(self):
        v = self.engine.evaluate(order(quantity=0), self.ctx())
        self.assertEqual(v.decision, Decision.BLOCK)
        self.assertFalse(v.overridable)
        self.assertEqual(self.result(v, "field_validation").status, ControlStatus.BLOCK)

    def test_limit_order_without_price_blocks(self):
        v = self.engine.evaluate(order(order_type=OrderType.LIMIT, limit_price=None), self.ctx())
        self.assertEqual(self.result(v, "field_validation").status, ControlStatus.BLOCK)
        self.assertEqual(v.decision, Decision.BLOCK)

    # ---- kill switch (hard) ----
    def test_kill_switch_blocks_hard_not_overridable(self):
        self.store.set_kill("RIA", True, "tester")
        v = self.engine.evaluate(order(), self.ctx())
        self.assertEqual(v.decision, Decision.BLOCK)
        self.assertFalse(v.overridable)
        self.assertEqual(self.result(v, "kill_switch").status, ControlStatus.BLOCK)

    def test_kill_switch_is_per_tenant(self):
        self.store.set_kill("BD", True, "tester")
        v = self.engine.evaluate(order(tenant="RIA"), self.ctx())  # RIA not killed
        self.assertEqual(self.result(v, "kill_switch").status, ControlStatus.PASS)

    # ---- price collar (soft) ----
    def test_through_collar_limit_soft_blocks_and_is_overridable(self):
        v = self.engine.evaluate(order(order_type=OrderType.LIMIT, limit_price=Decimal("260.00")), self.ctx())
        self.assertEqual(v.decision, Decision.BLOCK)
        self.assertTrue(v.overridable)
        self.assertEqual(self.result(v, "price_collar").status, ControlStatus.BLOCK)

    def test_limit_within_collar_passes(self):
        v = self.engine.evaluate(order(order_type=OrderType.LIMIT, limit_price=Decimal("228.00")), self.ctx())
        self.assertEqual(self.result(v, "price_collar").status, ControlStatus.PASS)
        self.assertEqual(v.decision, Decision.ALLOW)

    def test_stale_quote_fails_closed(self):
        self.store.set_quote_as_of("AAPL", self.now - timedelta(hours=16))
        v = self.engine.evaluate(order(), self.ctx())
        pc = self.result(v, "price_collar")
        self.assertEqual(pc.status, ControlStatus.BLOCK)
        self.assertIn("data_stale", pc.reason)

    def test_missing_quote_fails_closed(self):
        v = self.engine.evaluate(order(symbol="UNKNOWN"), self.ctx())
        self.assertEqual(self.result(v, "price_collar").status, ControlStatus.BLOCK)

    def test_future_dated_quote_is_not_trusted(self):
        self.store.set_quote_as_of("AAPL", self.now + timedelta(hours=1))
        self.assertEqual(self.result(self.engine.evaluate(order(), self.ctx()), "price_collar").status,
                         ControlStatus.BLOCK)

    # ---- duplicate / rate guard (hard) ----
    def test_duplicate_guard_passes_below_threshold(self):
        o = order(symbol="MSFT", quantity=500)
        self.store.insert_order(o, "allow", False, "ACCEPTED", "{}", "tester", self.now)
        v = self.engine.evaluate(o, self.ctx())
        self.assertEqual(self.result(v, "duplicate_rate_guard").status, ControlStatus.PASS)

    def test_duplicate_guard_blocks_at_threshold_hard(self):
        o = order(symbol="MSFT", quantity=500)
        self.store.insert_order(o, "allow", False, "ACCEPTED", "{}", "tester", self.now)
        self.store.insert_order(o, "allow", False, "ACCEPTED", "{}", "tester", self.now)
        v = self.engine.evaluate(o, self.ctx())  # this is the 3rd identical
        self.assertEqual(self.result(v, "duplicate_rate_guard").status, ControlStatus.BLOCK)
        self.assertEqual(v.decision, Decision.BLOCK)
        self.assertFalse(v.overridable)

    def test_duplicate_guard_ignores_orders_outside_window(self):
        o = order(symbol="MSFT", quantity=500)
        old = self.now - timedelta(seconds=120)  # outside the 60s window
        self.store.insert_order(o, "allow", False, "ACCEPTED", "{}", "tester", old)
        self.store.insert_order(o, "allow", False, "ACCEPTED", "{}", "tester", old)
        self.assertEqual(self.result(self.engine.evaluate(o, self.ctx()), "duplicate_rate_guard").status,
                         ControlStatus.PASS)

    # ---- buying power (audit-only) ----
    def test_over_buying_power_is_audit_not_blocking(self):
        v = self.engine.evaluate(order(symbol="NVDA", quantity=5000), self.ctx())  # notional >> cash
        bp = self.result(v, "buying_power")
        self.assertEqual(bp.status, ControlStatus.AUDIT)
        self.assertEqual(v.decision, Decision.ALLOW)  # audit never blocks on its own

    def test_sell_is_not_cash_constrained(self):
        v = self.engine.evaluate(order(side=Side.SELL), self.ctx())
        self.assertEqual(self.result(v, "buying_power").status, ControlStatus.PASS)

    # ---- composition precedence ----
    def test_hard_block_takes_precedence_over_soft(self):
        self.store.set_kill("RIA", True, "tester")
        v = self.engine.evaluate(order(order_type=OrderType.LIMIT, limit_price=Decimal("260.00")), self.ctx())
        self.assertEqual(v.decision, Decision.BLOCK)
        self.assertFalse(v.overridable)  # hard wins, so not overridable even though collar also soft-blocked

    def test_every_control_runs_and_is_reported(self):
        names = {c.name for c in self.engine.evaluate(order(), self.ctx()).controls}
        self.assertEqual(names, {"field_validation", "kill_switch", "price_collar",
                                 "duplicate_rate_guard", "buying_power", "credit_limit"})


if __name__ == "__main__":
    unittest.main()
