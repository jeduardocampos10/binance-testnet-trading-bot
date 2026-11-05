from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.analytics import build_analytics
from app.models import DAY_MS
from app.paper import run_paper_cycle
from app.state import AuditStore
from app.strategy import Decision
from tests.helpers import settings
from tests.test_paper import bar, entry_decision, report


class AnalyticsTests(unittest.TestCase):
    def test_builds_period_and_asset_performance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "analytics.sqlite3")
            self.addCleanup(store.close)
            config = settings()
            first = 701 * DAY_MS
            run_paper_cycle(
                report(
                    first,
                    (entry_decision("AAAUSDT", "100", first - 1),),
                    (bar("AAAUSDT", 700, "100", "101", "99", "100"),),
                ),
                config,
                store,
            )
            second = 702 * DAY_MS
            exit_decision = Decision(
                signal="EXIT_CANDIDATE",
                symbol="AAAUSDT",
                strategy_version="test-v1",
                candle_close_time=second - 1,
                reasons=("test exit",),
                metrics={"close": Decimal("110")},
                risk_plan=None,
            )
            run_paper_cycle(
                report(
                    second,
                    (exit_decision,),
                    (bar("AAAUSDT", 701, "105", "111", "104", "110"),),
                ),
                config,
                store,
            )
            result = build_analytics(store, "all")
            self.assertEqual(result["closed_trades"], 1)
            self.assertEqual(result["wins"], 1)
            self.assertEqual(result["best_asset"]["symbol"], "AAAUSDT")
            self.assertGreater(Decimal(result["realized_pnl_usdt"]), Decimal("0"))
            self.assertFalse(result["testnet_orders_enabled"])

    def test_empty_database_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "empty.sqlite3")
            self.addCleanup(store.close)
            result = build_analytics(store, "month")
            self.assertEqual(result["reports"], 0)
            self.assertEqual(result["closed_trades"], 0)
            self.assertEqual(result["period_pnl_usdt"], "0")

    def test_calendar_periods_do_not_include_an_extra_boundary_day(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "calendar.sqlite3")
            self.addCleanup(store.close)
            noon_ms = 12 * 60 * 60 * 1000
            for day in range(100, 108):
                generated = day * DAY_MS + noon_ms
                payload = {
                    "generated_at_ms": generated,
                    "risk_day_utc": f"1970-04-{day - 90:02d}",
                    "equity_usdt": "100",
                    "daily_pnl_usdt": "0",
                }
                store.record_paper_report(generated, payload["risk_day_utc"], payload)
            self.assertEqual(build_analytics(store, "day")["reports"], 1)
            self.assertEqual(build_analytics(store, "week")["reports"], 7)

    def test_rejects_non_finite_stored_financial_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "corrupt.sqlite3")
            self.addCleanup(store.close)
            payload = {
                "generated_at_ms": 100 * DAY_MS,
                "risk_day_utc": "1970-04-11",
                "equity_usdt": "NaN",
                "daily_pnl_usdt": "0",
            }
            store.record_paper_report(payload["generated_at_ms"], payload["risk_day_utc"], payload)
            with self.assertRaises(ValueError):
                build_analytics(store, "all")

    def test_rejects_unknown_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "period.sqlite3")
            self.addCleanup(store.close)
            with self.assertRaises(ValueError):
                build_analytics(store, "year")


if __name__ == "__main__":
    unittest.main()
