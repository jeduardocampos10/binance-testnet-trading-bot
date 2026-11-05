from __future__ import annotations

import unittest
from decimal import Decimal

from app.sessions import analyze_sessions
from tests.helpers import hourly_candles


class SessionAnalysisTests(unittest.TestCase):
    def test_reports_local_hours_with_minimum_samples(self) -> None:
        profile = analyze_sessions(
            "BTCUSDT",
            hourly_candles(480),
            spread_pct=Decimal("0.1"),
            timezone="America/Sao_Paulo",
            minimum_samples=5,
        )
        self.assertEqual(profile.timezone, "America/Sao_Paulo")
        self.assertEqual(len(profile.favorable_hours), 3)
        self.assertTrue(all(item.samples >= 5 for item in profile.favorable_hours))
        self.assertTrue(all(0 <= item.local_hour <= 23 for item in profile.favorable_hours))

    def test_returns_no_favorable_hour_when_sample_is_too_small(self) -> None:
        profile = analyze_sessions(
            "BTCUSDT",
            hourly_candles(24),
            spread_pct=Decimal("0.1"),
            timezone="America/Sao_Paulo",
            minimum_samples=2,
        )
        self.assertEqual(profile.favorable_hours, ())


if __name__ == "__main__":
    unittest.main()
