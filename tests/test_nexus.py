from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from app.config import settings_fingerprint
from app.models import DAY_MS
from app.nexus import _hash, build_nexus_review
from app.paper import run_paper_cycle
from app.probability import GateEvidence
from app.state import AuditStore, _payload_hash
from tests.helpers import settings
from tests.test_paper import bar, entry_decision, no_entry_decision, report


class MaxxNexusTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = AuditStore(Path(directory.name) / "nexus.sqlite3")
        self.addCleanup(self.store.close)

    @staticmethod
    def _off_evidence() -> GateEvidence:
        return GateEvidence(
            "OFF", None, 0, 0, None, 0.65, 0.55, 50, True,
            "probability gate disabled", None,
        )

    def test_review_is_deterministic_and_external_context_fails_closed(self) -> None:
        config = replace(settings(), nexus_mode="AUDIT")
        decision = entry_decision("AAAUSDT", "100", DAY_MS - 1)
        kwargs = dict(
            paused=False,
            day_locked=False,
            open_symbols=(),
            open_count=0,
            cash=Decimal("100"),
            allocation=Decimal("10.01"),
            selected_by_probability=True,
        )
        first = build_nexus_review(
            decision, self._off_evidence(), config, settings_fingerprint(config), **kwargs
        )
        second = build_nexus_review(
            decision, self._off_evidence(), config, settings_fingerprint(config), **kwargs
        )
        self.assertEqual(first["review_hash"], second["review_hash"])
        self.assertEqual(first["disposition"], "SHADOW_BLOCK")
        self.assertIn("EXTERNAL_CONTEXT_NOT_CONFIGURED", first["reason_codes"])
        unavailable = [item for item in first["reports"] if item["status"] == "NOT_CONFIGURED"]
        self.assertEqual(len(unavailable), 6)
        self.assertTrue(all(not item["evidence"]["treated_as_positive"] for item in unavailable))

    def test_audit_records_shadow_review_without_changing_paper_entry(self) -> None:
        config = replace(settings(), nexus_mode="AUDIT")
        timestamp = 101 * DAY_MS
        payload = run_paper_cycle(
            report(
                timestamp,
                (entry_decision("AAAUSDT", "100", timestamp - 1),),
                (bar("AAAUSDT", 100, "100", "101", "99", "100"),),
            ),
            config,
            self.store,
        )
        self.assertEqual(len(payload["open_positions"]), 1)
        status = self.store.nexus_status()
        self.assertEqual(status["review_count"], 1)
        self.assertEqual(status["reviews"][0]["disposition"], "SHADOW_BLOCK")
        self.assertIsNone(status["reviews"][0]["permit"])

    def test_nexus_status_detects_tampering_and_filters_counts(self) -> None:
        config = replace(settings(), nexus_mode="AUDIT")
        timestamp = 106 * DAY_MS
        run_paper_cycle(
            report(
                timestamp,
                (
                    entry_decision("AAAUSDT", "100", timestamp - 1),
                    entry_decision("BBBUSDT", "100", timestamp - 1),
                ),
                (
                    bar("AAAUSDT", 105, "100", "101", "99", "100"),
                    bar("BBBUSDT", 105, "100", "101", "99", "100"),
                ),
            ),
            config,
            self.store,
        )
        filtered = self.store.nexus_status("AAAUSDT")
        self.assertEqual(filtered["review_count"], 1)
        self.store._connection.execute(
            "UPDATE nexus_reviews SET payload_json = ? WHERE symbol = ?",
            ('{"review_hash":"forged"}', "AAAUSDT"),
        )
        self.store._connection.commit()
        self.assertEqual(self.store.nexus_status("AAAUSDT")["reviews"][0]["status"], "INTEGRITY_ERROR")

    def test_issued_permit_linked_to_position_is_integrity_error(self) -> None:
        config = replace(settings(), nexus_mode="ENFORCE")
        decision = entry_decision("AAAUSDT", "100", DAY_MS - 1)
        review = build_nexus_review(
            decision, self._off_evidence(), config, settings_fingerprint(config),
            paused=False, day_locked=False, open_symbols=(), open_count=0,
            cash=Decimal("100"), allocation=Decimal("10.01"), selected_by_probability=True,
        )
        review["disposition"] = "PERMIT_ELIGIBLE"
        unsigned_review = dict(review)
        unsigned_review.pop("review_hash")
        review["review_hash"] = _hash(unsigned_review)
        text = json.dumps(review, sort_keys=True, separators=(",", ":"))
        cursor = self.store._connection.execute(
            """INSERT INTO nexus_reviews
                   (candidate_key, symbol, candle_close_time, mode, disposition, review_hash, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                review["candidate_key"], review["symbol"], review["candle_close_time"],
                review["mode"], review["disposition"], review["review_hash"], text,
            ),
        )
        review_id = int(cursor.lastrowid)
        permit = {
            "schema_version": 1, "operation": "OPEN_PAPER_POSITION",
            "execution_mode": "PAPER", "symbol": "AAAUSDT",
            "candle_close_time": DAY_MS - 1, "review_hash": review["review_hash"],
            "orders_enabled": False,
        }
        permit["permit_hash"] = _payload_hash(permit, "permit_hash")
        cursor = self.store._connection.execute(
            """INSERT INTO paper_permits
                   (review_id, symbol, candle_close_time, status, payload_json)
               VALUES (?, ?, ?, 'ISSUED', ?)""",
            (review_id, "AAAUSDT", DAY_MS - 1, json.dumps(permit, sort_keys=True, separators=(",", ":"))),
        )
        permit_id = int(cursor.lastrowid)
        position_cursor = self.store._connection.execute(
            """INSERT INTO paper_positions
                   (symbol, entry_day, entry_time_ms, entry_candle_close_time, entry_price,
                    quantity, entry_fee, hard_stop, peak_price, last_mark, nexus_permit_id, status)
               VALUES ('AAAUSDT', '1970-01-01', 1, ?, '100', '0.1', '0.01', '95',
                       '100', '100', ?, 'OPEN')""",
            (DAY_MS - 1, permit_id),
        )
        position_id = int(position_cursor.lastrowid)
        self.store._connection.commit()
        status = self.store.nexus_status()
        self.assertEqual(status["permit_integrity_error_count"], 1)
        self.assertEqual(status["full_chain_integrity_ok_count"], 0)
        self.assertEqual(status["reviews"][0]["status"], "INTEGRITY_ERROR")
        self.store._connection.execute(
            """UPDATE paper_permits SET status = 'CONSUMED', consumed_position_id = ?
               WHERE id = ?""",
            (position_id, permit_id),
        )
        self.store._connection.commit()
        valid_chain = self.store.nexus_status()
        self.assertEqual(valid_chain["permit_integrity_ok_count"], 1)
        self.assertEqual(valid_chain["full_chain_integrity_ok_count"], 1)

        self.store._connection.execute(
            "UPDATE nexus_reviews SET mode = 'AUDIT' WHERE id = ?", (review_id,)
        )
        self.store._connection.commit()
        corrupted_review = self.store.nexus_status()
        self.assertEqual(corrupted_review["review_integrity_error_count"], 1)
        self.assertEqual(corrupted_review["permit_integrity_error_count"], 1)
        self.assertEqual(corrupted_review["full_chain_integrity_ok_count"], 0)

        self.store._connection.execute(
            "UPDATE nexus_reviews SET mode = 'ENFORCE' WHERE id = ?", (review_id,)
        )
        self.store._connection.execute(
            "UPDATE paper_permits SET consumed_position_id = 999 WHERE id = ?", (permit_id,)
        )
        self.store._connection.commit()
        consumed_wrong = self.store.nexus_status()
        self.assertEqual(consumed_wrong["permit_integrity_error_count"], 1)
        self.assertEqual(consumed_wrong["full_chain_integrity_ok_count"], 0)

    def test_nexus_mode_cannot_be_downgraded_on_same_database(self) -> None:
        audit = replace(settings(), nexus_mode="AUDIT")
        first = 111 * DAY_MS
        first_report = report(
            first,
            (entry_decision("AAAUSDT", "100", first - 1),),
            (bar("AAAUSDT", 110, "100", "101", "99", "100"),),
        )
        run_paper_cycle(first_report, audit, self.store)
        enforce = replace(settings(), nexus_mode="ENFORCE")
        replay = run_paper_cycle(first_report, enforce, self.store)
        self.assertEqual(replay["maxx_nexus"]["mode"], "AUDIT")
        self.assertEqual(self.store.paper_account()["nexus_mode"], "ENFORCE")
        with self.assertRaisesRegex(ValueError, "downgrade"):
            run_paper_cycle(first_report, audit, self.store)
        with self.assertRaisesRegex(ValueError, "downgrade"):
            run_paper_cycle(first_report, settings(), self.store)
        second = 112 * DAY_MS
        with self.assertRaisesRegex(ValueError, "downgrade"):
            run_paper_cycle(
                report(
                    second,
                    (no_entry_decision("AAAUSDT", "100", second - 1),),
                    (bar("AAAUSDT", 111, "100", "101", "99", "100"),),
                ),
                settings(),
                self.store,
            )

    def test_enforce_blocks_without_required_external_context(self) -> None:
        config = replace(settings(), nexus_mode="ENFORCE")
        timestamp = 121 * DAY_MS
        payload = run_paper_cycle(
            report(
                timestamp,
                (entry_decision("AAAUSDT", "100", timestamp - 1),),
                (bar("AAAUSDT", 120, "100", "101", "99", "100"),),
            ),
            config,
            self.store,
        )
        self.assertEqual(payload["open_positions"], [])
        self.assertIn("ENTRY_BLOCKED_NEXUS", [item["event_type"] for item in payload["events_today"]])
        self.assertEqual(self.store.nexus_status()["reviews"][0]["disposition"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
