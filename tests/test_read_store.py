from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.read_store import DashboardReadStore
from app.state import AuditStore


class DashboardReadStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db_path = Path(directory.name) / "paper.sqlite3"
        store = AuditStore(self.db_path)
        store.close()
        connection = sqlite3.connect(self.db_path)
        connection.execute(
            "INSERT INTO paper_account (id, initial_equity, cash, config_hash, nexus_mode) VALUES (1, ?, ?, ?, ?)",
            ("10000", "9000", "cfg", "REQUIRED"),
        )
        connection.execute(
            "INSERT INTO paper_days (day_utc, start_equity, locked, lock_reason) VALUES (?, ?, 0, NULL)",
            ("2026-01-01", "10000"),
        )
        connection.execute(
            """INSERT INTO paper_positions
               (symbol, entry_day, entry_time_ms, entry_price, quantity, entry_fee,
                hard_stop, trailing_stop, peak_price, last_mark, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
            ("BTCUSDT", "2026-01-01", 1000, "100", "2", "1", "90", "95", "112", "110"),
        )
        entry = {
            "reference_price": "100", "fill_price": "101", "quantity": "2",
            "gross_usdt": "202", "fee_usdt": "1",
        }
        connection.execute(
            """INSERT INTO paper_events
               (event_key, day_utc, generated_at_ms, event_type, symbol, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            ("entry:1", "2026-01-01", 1000, "PAPER_ENTRY", "BTCUSDT", json.dumps(entry)),
        )
        report = {
            "equity_usdt": "10199", "cash_usdt": "9000", "daily_pnl_usdt": "199",
            "daily_pnl_pct": "1.99", "probability_gate_mode": "required", "risk_day_utc": "2026-01-01",
        }
        connection.execute(
            "INSERT INTO paper_reports (generated_at_ms, day_utc, payload_json) VALUES (?, ?, ?)",
            (1000, "2026-01-01", json.dumps(report)),
        )
        connection.commit()
        connection.close()

    def test_orders_are_explicitly_simulated_and_never_sent(self) -> None:
        with DashboardReadStore(self.db_path) as store:
            result = store.orders()
        self.assertEqual(result["execution_mode"], "PAPER")
        self.assertFalse(result["orders_enabled"])
        self.assertIn("NÃO ENVIADA", result["notice"])
        self.assertEqual(result["items"][0]["status"], "FILLED_SIMULATED")
        self.assertFalse(result["items"][0]["sent_to_binance"])

    def test_positions_and_risk_are_derived_read_only(self) -> None:
        with DashboardReadStore(self.db_path) as store:
            positions = store.positions(status="open", fee_per_side_pct="0.1")
            risk = store.risk(
                max_positions=5,
                max_exposure_pct="20",
                daily_loss_limit_pct="3",
                fee_per_side_pct="0.1",
            )
        position = positions["items"][0]
        self.assertEqual(position["exposure_usdt"], "220")
        self.assertEqual(position["unrealized_pnl_usdt"], "18.780")
        self.assertEqual(position["risk_to_stop_usdt"], "30")
        self.assertEqual(risk["gross_exposure_usdt"], "220")
        self.assertEqual(risk["open_positions"], 1)
        self.assertFalse(risk["orders_enabled"])

    def test_store_does_not_create_a_missing_database(self) -> None:
        missing = self.db_path.with_name("missing.sqlite3")
        with self.assertRaises(sqlite3.OperationalError):
            DashboardReadStore(missing)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
