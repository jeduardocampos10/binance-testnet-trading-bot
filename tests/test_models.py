from __future__ import annotations

import unittest
from decimal import Decimal

from app.models import DAY_MS, DataValidationError, validate_daily_candles
from tests.helpers import candles_from_closes


class CandleValidationTests(unittest.TestCase):
    def test_accepts_closed_contiguous_daily_candles(self) -> None:
        candles = candles_from_closes([Decimal("100"), Decimal("101")])
        result = validate_daily_candles(candles, now_ms=2 * DAY_MS)
        self.assertEqual(result, candles)

    def test_rejects_open_candle(self) -> None:
        candles = candles_from_closes([Decimal("100")])
        with self.assertRaises(DataValidationError):
            validate_daily_candles(candles, now_ms=DAY_MS - 1)

    def test_rejects_gap(self) -> None:
        candles = candles_from_closes([Decimal("100"), Decimal("101"), Decimal("102")])
        candles = [candles[0], candles[2]]
        with self.assertRaises(DataValidationError):
            validate_daily_candles(candles, now_ms=4 * DAY_MS)

    def test_rejects_stale_latest_candle(self) -> None:
        candles = candles_from_closes([Decimal("100"), Decimal("101")])
        with self.assertRaises(DataValidationError):
            validate_daily_candles(candles, now_ms=3 * DAY_MS)

    def test_rejects_non_finite_candle_value(self) -> None:
        from app.models import Candle

        with self.assertRaises(DataValidationError):
            Candle(
                open_time=0,
                close_time=DAY_MS - 1,
                open=Decimal("Infinity"),
                high=Decimal("Infinity"),
                low=Decimal("100"),
                close=Decimal("100"),
                volume=Decimal("1"),
            )


if __name__ == "__main__":
    unittest.main()
