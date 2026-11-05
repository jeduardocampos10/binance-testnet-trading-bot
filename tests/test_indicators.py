from __future__ import annotations

import unittest
from decimal import Decimal

from app.indicators import atr, ema_series, rsi, sma
from tests.helpers import candles_from_closes


class IndicatorTests(unittest.TestCase):
    def test_sma(self) -> None:
        self.assertEqual(sma([Decimal("1"), Decimal("2"), Decimal("3")], 2), Decimal("2.5"))

    def test_flat_ema_stays_flat(self) -> None:
        result = ema_series([Decimal("10")] * 20, 7)
        self.assertTrue(all(value == Decimal("10") for value in result))

    def test_flat_rsi_is_neutral(self) -> None:
        self.assertEqual(rsi([Decimal("10")] * 15), Decimal("50"))

    def test_atr_is_positive(self) -> None:
        candles = candles_from_closes([Decimal("100")] * 20)
        self.assertEqual(atr(candles, 14), Decimal("2.00"))


if __name__ == "__main__":
    unittest.main()
