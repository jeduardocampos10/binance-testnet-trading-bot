from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from app.manual_analysis import build_manual_analysis
from app.models import DAY_MS
from app.observer import observe_market
from app.openai_advisor import AdvisorResult
from app.state import AuditStore
from tests.helpers import settings
from tests.test_observer import FakeMarketClient


class FakeAdvisor:
    model = "gpt-test"
    def __init__(self, verdict="OPPORTUNITY"): self.verdict = verdict
    def assess(self, value):
        return AdvisorResult(self.verdict, .9, "assessment", ("trend",), (), ("stop",), (), self.model, "a" * 64)


class ManualAnalysisTests(unittest.TestCase):
    def report(self):
        return observe_market(settings(), client=FakeMarketClient(), now_ms=100 * DAY_MS)

    def test_only_fresh_top10_and_fail_closed(self) -> None:
        report = self.report()
        with self.assertRaises(ValueError):
            build_manual_analysis(report, "NOTUSDT", advisor=FakeAdvisor())
        payload = build_manual_analysis(report, report.ranking[0].symbol, advisor=FakeAdvisor())
        self.assertNotEqual(payload["final_verdict"], "OPPORTUNITY")
        self.assertFalse(payload["orders_enabled"])
        self.assertTrue(all(item["status"] == "PASS" for item in payload["integrity_checks"]))

    def test_inconsistent_corresponding_data_is_inconclusive(self) -> None:
        report = self.report(); symbol = report.ranking[0].symbol
        broken = replace(report, session_profiles=tuple(p for p in report.session_profiles if p.symbol != symbol))
        payload = build_manual_analysis(broken, symbol, advisor=FakeAdvisor())
        self.assertEqual(payload["final_verdict"], "INCONCLUSIVE")
        self.assertEqual(payload["ai"]["status"], "SKIPPED_INTEGRITY_FAILURE")

    def test_persistence_is_idempotent_and_queryable(self) -> None:
        payload = build_manual_analysis(self.report(), "AAAUSDT")
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "audit.sqlite3")
            try:
                first, inserted = store.record_manual_analysis(payload)
                second, inserted_again = store.record_manual_analysis(payload)
                self.assertTrue(inserted); self.assertFalse(inserted_again); self.assertEqual(first, second)
                self.assertEqual(store.manual_analysis_by_id(first)["symbol"], "AAAUSDT")
                self.assertEqual(len(store.list_manual_analyses("AAAUSDT")), 1)
            finally: store.close()


if __name__ == "__main__": unittest.main()
