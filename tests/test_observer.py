from __future__ import annotations

import unittest
from decimal import Decimal

from app.market_data import UniverseSymbol
from app.observer import observe_market
from app.models import DAY_MS, HOUR_MS
from tests.helpers import candles_from_closes, hourly_candles, settings


class FakeMarketClient:
    def eligible_symbols(self, quote_asset: str, maximum_position_usdt: Decimal, excluded_base_assets=()):
        return [
            UniverseSymbol("AAAUSDT", "AAA", "USDT", Decimal("5")),
            UniverseSymbol("BBBUSDT", "BBB", "USDT", Decimal("5")),
        ]

    def quote_volumes_24h(self):
        return {"AAAUSDT": Decimal("1000000"), "BBBUSDT": Decimal("500000")}

    def spreads_pct(self):
        return {"AAAUSDT": Decimal("0.1"), "BBBUSDT": Decimal("0.2")}

    def daily_candles(self, symbol: str, limit: int):
        if symbol == "AAAUSDT":
            return candles_from_closes([Decimal(100 + index) for index in range(100)])
        return candles_from_closes([Decimal("100")] * 100)

    def hourly_candles(self, symbol: str, limit: int):
        return hourly_candles(limit, start=100 * DAY_MS - limit * HOUR_MS)


class ObserverTests(unittest.TestCase):
    def test_builds_top_market_report_without_orders(self) -> None:
        report = observe_market(settings(), client=FakeMarketClient(), now_ms=100 * DAY_MS)
        self.assertEqual(report.mode, "OBSERVATION")
        self.assertEqual(report.universe_count, 2)
        self.assertEqual(report.analyzed_count, 2)
        self.assertEqual(report.ranking[0].symbol, "AAAUSDT")
        self.assertEqual(len(report.decisions), 2)
        self.assertEqual(len(report.market_bars), 2)
        self.assertEqual(len(report.session_profiles), 2)


class HeldSymbolManagementTests(unittest.TestCase):
    def test_held_symbol_is_managed_even_outside_entry_universe(self) -> None:
        report = observe_market(
            settings(),
            client=FakeMarketClient(),
            now_ms=100 * DAY_MS,
            required_symbols=("CCCUSDT",),
        )
        self.assertIn("CCCUSDT", {bar.symbol for bar in report.market_bars})
        self.assertIn("CCCUSDT", {decision.symbol for decision in report.decisions})
        self.assertIn("CCCUSDT", report.session_skipped_symbols)


class PublicAnalysisClient:
    def quote_volumes_24h(self):
        return {"AAAUSDT": Decimal("1000000"), "ZZZUSDT": Decimal("9000000")}

    def spreads_pct(self):
        return {"AAAUSDT": Decimal("0.1"), "ZZZUSDT": Decimal("0.1")}

    def daily_candles(self, symbol: str, limit: int):
        return candles_from_closes([Decimal(100 + index) for index in range(100)])

    def hourly_candles(self, symbol: str, limit: int):
        return hourly_candles(limit, start=100 * DAY_MS - limit * HOUR_MS)


class TestnetEligibilityClient:
    def eligible_symbols(self, quote_asset: str, maximum_position_usdt: Decimal, excluded_base_assets=()):
        return [UniverseSymbol("AAAUSDT", "AAA", "USDT", Decimal("5"))]


class SplitSourceTests(unittest.TestCase):
    def test_public_analysis_is_intersected_with_testnet_universe(self) -> None:
        result = observe_market(
            settings(),
            client=PublicAnalysisClient(),
            testnet_client=TestnetEligibilityClient(),
            now_ms=100 * DAY_MS,
        )
        self.assertEqual(result.market_data_source, "BINANCE_PUBLIC_PRODUCTION")
        self.assertEqual(result.future_execution_target, "BINANCE_SPOT_TESTNET")
        self.assertEqual({bar.symbol for bar in result.market_bars}, {"AAAUSDT"})
        self.assertNotIn("ZZZUSDT", {asset.symbol for asset in result.ranking})


if __name__ == "__main__":
    unittest.main()
