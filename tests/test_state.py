from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from app.state import AuditStore
from app.strategy import Decision


class AuditStoreTests(unittest.TestCase):
    def test_decision_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AuditStore(Path(directory) / "audit.sqlite3")
            self.addCleanup(store.close)
            decision = Decision(
                signal="NO_ENTRY",
                symbol="BTCUSDT",
                strategy_version="test-v1",
                candle_close_time=123,
                reasons=("test",),
                metrics={"close": Decimal("100")},
                risk_plan=None,
            )
            self.assertTrue(store.record(decision))
            self.assertFalse(store.record(decision))
            latest = store.latest()
            assert latest is not None
            self.assertEqual(latest["signal"], "NO_ENTRY")
            self.assertEqual(latest["mode"], "OBSERVATION")


if __name__ == "__main__":
    unittest.main()
