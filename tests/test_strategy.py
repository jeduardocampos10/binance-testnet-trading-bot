from __future__ import annotations

import unittest
from decimal import Decimal

from app.strategy import _risk_plan, evaluate
from tests.helpers import candles_from_closes, settings


class StrategyTests(unittest.TestCase):
    def test_does_not_force_entry_in_flat_market(self) -> None:
        decision = evaluate("BTCUSDT", candles_from_closes([Decimal("100")] * 90), settings())
        self.assertEqual(decision.signal, "NO_ENTRY")
        self.assertIsNone(decision.risk_plan)

    def test_emits_exit_candidate_in_declining_market(self) -> None:
        closes = [Decimal(200 - index) for index in range(90)]
        decision = evaluate("BTCUSDT", candles_from_closes(closes), settings())
        self.assertEqual(decision.signal, "EXIT_CANDIDATE")
        self.assertIsNone(decision.risk_plan)

    def test_risk_plan_caps_exposure(self) -> None:
        candles = candles_from_closes([Decimal("100")] * 90)
        plan, rejection = _risk_plan(candles, settings(), Decimal("100"), Decimal("2"))
        self.assertIsNone(rejection)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertLessEqual(plan.planned_position_usdt, Decimal("10"))
        self.assertEqual(plan.risk_budget_usdt, Decimal("0.5"))
        self.assertGreaterEqual(plan.reward_risk_ratio, Decimal("2"))

    def test_rejects_stop_that_is_too_far(self) -> None:
        candles = candles_from_closes([Decimal("100")] * 90)
        plan, rejection = _risk_plan(candles, settings(), Decimal("100"), Decimal("3"))
        self.assertIsNone(plan)
        self.assertIn("ATR", rejection or "")


if __name__ == "__main__":
    unittest.main()
