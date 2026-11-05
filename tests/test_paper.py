from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from app.models import DAY_MS
from app.observer import MarketBar, ObservationReport
from app.paper import run_paper_cycle
from app.ranking import RankedAsset
from app.state import AuditStore
from app.strategy import Decision, RiskPlan
from tests.helpers import settings


def entry_decision(symbol: str, price: str, close_time: int) -> Decision:
    value = Decimal(price)
    plan = RiskPlan(
        entry=value,
        stop=value * Decimal("0.95"),
        target=value * Decimal("1.10"),
        stop_distance_pct=Decimal("5"),
        risk_budget_usdt=Decimal("0.5"),
        maximum_position_usdt=Decimal("10"),
        planned_position_usdt=Decimal("10"),
        quantity=Decimal("10") / value,
        reward_risk_ratio=Decimal("2"),
    )
    return Decision(
        signal="ENTRY_CANDIDATE",
        symbol=symbol,
        strategy_version="test-v1",
        candle_close_time=close_time,
        reasons=("test entry",),
        metrics={"close": value},
        risk_plan=plan,
    )


def no_entry_decision(symbol: str, price: str, close_time: int) -> Decision:
    return Decision(
        signal="NO_ENTRY",
        symbol=symbol,
        strategy_version="test-v1",
        candle_close_time=close_time,
        reasons=("test",),
        metrics={"close": Decimal(price)},
        risk_plan=None,
    )


def report(
    timestamp: int,
    decisions: tuple[Decision, ...],
    bars: tuple[MarketBar, ...],
    *,
    include_entry_ranking: bool = True,
) -> ObservationReport:
    ranked_entries = tuple(
        RankedAsset(
            rank=index,
            symbol=decision.symbol,
            score=Decimal("1"),
            month_return_pct=Decimal("1"),
            return_30d_pct=Decimal("1"),
            maximum_drawdown_30d_pct=Decimal("0"),
            quote_volume_24h=Decimal("1000000"),
            spread_pct=Decimal("0.1"),
            trend_eligible=True,
            entry_eligible=True,
            eligibility_reasons=(),
        )
        for index, decision in enumerate(
            (item for item in decisions if item.signal == "ENTRY_CANDIDATE"), start=1
        )
    ) if include_entry_ranking else ()
    return ObservationReport(
        generated_at_ms=timestamp,
        mode="OBSERVATION",
        strategy_version="test-v1",
        market_data_source="BINANCE_PUBLIC_PRODUCTION",
        future_execution_target="BINANCE_SPOT_TESTNET",
        universe_count=len(bars),
        prefiltered_count=len(bars),
        analyzed_count=len(bars),
        skipped_symbols=(),
        skipped_reasons={},
        ranking=ranked_entries,
        decisions=decisions,
        market_bars=bars,
        session_profiles=(),
        session_skipped_symbols=(),
    )


def bar(symbol: str, day: int, open_: str, high: str, low: str, close: str) -> MarketBar:
    start = day * DAY_MS
    return MarketBar(
        symbol=symbol,
        open_time=start,
        close_time=start + DAY_MS - 1,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


class PaperPortfolioTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = AuditStore(Path(directory.name) / "paper.sqlite3")
        self.addCleanup(self.store.close)
        self.settings = settings()

    def test_entry_trailing_stop_and_exit_include_costs(self) -> None:
        first = 101 * DAY_MS
        payload = run_paper_cycle(
            report(first, (entry_decision("AAAUSDT", "100", first - 1),), (bar("AAAUSDT", 100, "99", "101", "98", "100"),)),
            self.settings,
            self.store,
        )
        self.assertEqual(len(payload["open_positions"]), 1)
        self.assertLess(Decimal(payload["equity_usdt"]), Decimal("100"))
        # Replaying the same report must not apply pre-entry candle lows.
        replay = run_paper_cycle(
            report(first + 123, (entry_decision("AAAUSDT", "100", first - 1),), (bar("AAAUSDT", 100, "99", "101", "90", "100"),)),
            self.settings,
            self.store,
        )
        self.assertEqual(len(replay["open_positions"]), 1)

        second = 102 * DAY_MS
        run_paper_cycle(
            report(second, (no_entry_decision("AAAUSDT", "108", second - 1),), (bar("AAAUSDT", 101, "100", "110", "99", "108"),)),
            self.settings,
            self.store,
        )
        position = self.store.open_positions()[0]
        self.assertEqual(Decimal(position["trailing_stop"]), Decimal("106.70"))
        # A replay with another wall-clock time cannot reapply the same low
        # against the trailing stop that this candle just created.
        run_paper_cycle(
            report(second + 456, (no_entry_decision("AAAUSDT", "108", second - 1),), (bar("AAAUSDT", 101, "100", "110", "100", "108"),)),
            self.settings,
            self.store,
        )
        self.assertEqual(len(self.store.open_positions()), 1)

        third = 103 * DAY_MS
        payload = run_paper_cycle(
            report(third, (no_entry_decision("AAAUSDT", "106", third - 1),), (bar("AAAUSDT", 102, "106", "107", "105", "106"),)),
            self.settings,
            self.store,
        )
        self.assertEqual(payload["open_positions"], [])
        exits = [event for event in payload["events_today"] if event["event_type"] == "PAPER_EXIT"]
        self.assertEqual(exits[0]["payload"]["reason"], "PROTECTIVE_STOP")
        self.assertGreater(Decimal(exits[0]["payload"]["realized_pnl_usdt"]), Decimal("0"))

    def test_management_only_symbol_is_not_reopened_after_exit(self) -> None:
        first = 151 * DAY_MS
        run_paper_cycle(
            report(
                first,
                (entry_decision("AAAUSDT", "100", first - 1),),
                (bar("AAAUSDT", 150, "100", "101", "99", "100"),),
            ),
            self.settings,
            self.store,
        )
        second = 152 * DAY_MS
        payload = run_paper_cycle(
            report(
                second,
                (entry_decision("AAAUSDT", "90", second - 1),),
                (bar("AAAUSDT", 151, "90", "91", "89", "90"),),
                include_entry_ranking=False,
            ),
            self.settings,
            self.store,
        )
        self.assertEqual(payload["open_positions"], [])
        self.assertEqual(payload["entry_candidates"], [])

    def test_manual_pause_blocks_entry_but_records_simulation(self) -> None:
        self.store.set_paper_paused(True, "operator review")
        timestamp = 181 * DAY_MS
        payload = run_paper_cycle(
            report(
                timestamp,
                (entry_decision("AAAUSDT", "100", timestamp - 1),),
                (bar("AAAUSDT", 180, "100", "101", "99", "100"),),
            ),
            self.settings,
            self.store,
        )
        self.assertTrue(payload["entries_paused"])
        self.assertEqual(payload["open_positions"], [])
        self.assertIn(
            "PAUSED_ENTRY_SIMULATION",
            [event["event_type"] for event in payload["events_today"]],
        )

    def test_daily_loss_lock_simulates_new_entry_instead_of_opening_it(self) -> None:
        symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
        first = 201 * DAY_MS
        run_paper_cycle(
            report(
                first,
                tuple(entry_decision(symbol, "100", first - 1) for symbol in symbols),
                tuple(bar(symbol, 200, "100", "101", "99", "100") for symbol in symbols),
            ),
            self.settings,
            self.store,
        )
        self.assertEqual(len(self.store.open_positions()), 3)

        second = 202 * DAY_MS
        decisions = tuple(no_entry_decision(symbol, "90", second - 1) for symbol in symbols) + (
            entry_decision("DDDUSDT", "100", second - 1),
        )
        bars = tuple(bar(symbol, 201, "90", "91", "89", "90") for symbol in symbols) + (
            bar("DDDUSDT", 201, "100", "101", "99", "100"),
        )
        payload = run_paper_cycle(report(second, decisions, bars), self.settings, self.store)
        self.assertTrue(payload["daily_lock"])
        self.assertEqual(payload["open_positions"], [])
        simulations = [event for event in payload["events_today"] if event["event_type"] == "LOCKED_ENTRY_SIMULATION"]
        self.assertEqual([event["symbol"] for event in simulations], ["DDDUSDT"])
        self.assertFalse(payload["testnet_orders_enabled"])


class PaperOrderingTests(unittest.TestCase):
    def test_rejects_unseen_out_of_order_candle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "ordering.sqlite3")
            self.addCleanup(store.close)
            config = settings()
            recent_time = 301 * DAY_MS
            recent = report(
                recent_time,
                (entry_decision("AAAUSDT", "100", recent_time - 1),),
                (bar("AAAUSDT", 300, "100", "101", "99", "100"),),
            )
            run_paper_cycle(recent, config, store)

            old_time = 299 * DAY_MS
            stale = report(
                old_time,
                (no_entry_decision("AAAUSDT", "100", old_time - 1),),
                (bar("AAAUSDT", 298, "100", "101", "99", "100"),),
            )
            with self.assertRaisesRegex(ValueError, "stale|out-of-order"):
                run_paper_cycle(stale, config, store)

    def test_market_bar_rejects_malformed_ohlc(self) -> None:
        with self.assertRaisesRegex(ValueError, "OHLC|finite"):
            MarketBar(
                symbol="AAAUSDT",
                open_time=0,
                close_time=DAY_MS - 1,
                open=Decimal("100"),
                high=Decimal("90"),
                low=Decimal("80"),
                close=Decimal("100"),
            )

    def test_rejects_future_or_misaligned_market_bar(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "future.sqlite3")
            self.addCleanup(store.close)
            timestamp = 500 * DAY_MS
            future = report(
                timestamp,
                (entry_decision("AAAUSDT", "100", 501 * DAY_MS - 1),),
                (bar("AAAUSDT", 500, "100", "101", "99", "100"),),
            )
            with self.assertRaisesRegex(ValueError, "latest closed"):
                run_paper_cycle(future, settings(), store)

    def test_configuration_change_requires_a_new_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "config.sqlite3")
            self.addCleanup(store.close)
            config = settings()
            first_time = 601 * DAY_MS
            run_paper_cycle(
                report(
                    first_time,
                    (entry_decision("AAAUSDT", "100", first_time - 1),),
                    (bar("AAAUSDT", 600, "100", "101", "99", "100"),),
                ),
                config,
                store,
            )
            second_time = 602 * DAY_MS
            changed = replace(config, fee_per_side_pct=Decimal("0.2"))
            with self.assertRaisesRegex(ValueError, "configuration hash mismatch"):
                run_paper_cycle(
                    report(
                        second_time,
                        (no_entry_decision("AAAUSDT", "100", second_time - 1),),
                        (bar("AAAUSDT", 601, "100", "101", "99", "100"),),
                    ),
                    changed,
                    store,
                )


class PaperConcurrencyTests(unittest.TestCase):
    def test_two_workers_cannot_apply_one_candle_twice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "concurrent.sqlite3"
            initializer = AuditStore(path)
            initializer.close()
            config = settings()
            timestamp = 401 * DAY_MS
            cycle_report = report(
                timestamp,
                (entry_decision("AAAUSDT", "100", timestamp - 1),),
                (bar("AAAUSDT", 400, "100", "101", "99", "100"),),
            )

            def worker() -> dict[str, object]:
                store = AuditStore(path)
                try:
                    return run_paper_cycle(cycle_report, config, store)
                finally:
                    store.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda _: worker(), range(2)))
            self.assertEqual(results[0]["equity_usdt"], results[1]["equity_usdt"])
            verifier = AuditStore(path)
            self.addCleanup(verifier.close)
            self.assertEqual(len(verifier.open_positions()), 1)


if __name__ == "__main__":
    unittest.main()
