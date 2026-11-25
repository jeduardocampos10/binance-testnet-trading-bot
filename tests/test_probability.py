from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from app.models import DAY_MS
from app.paper import run_paper_cycle
from app.probability import (
    FEATURE_NAMES,
    _calibrated,
    artifact_hash,
    canonical_json,
    fit_artifact,
    load_artifact,
    pava,
    temporal_split,
    write_artifact,
)
from app.state import AuditStore
from tests.helpers import settings
from tests.test_paper import bar, entry_decision, no_entry_decision, report


def feature_metrics(price: str = "100") -> dict[str, Decimal]:
    close = Decimal(price)
    return {
        "close": close,
        "sma30": close * Decimal("0.99"),
        "sma60": close * Decimal("0.98"),
        "sma60_five_ago": close * Decimal("0.97"),
        "ema7": close * Decimal("1.01"),
        "ema15": close,
        "rsi14": Decimal("60"),
        "atr14": close * Decimal("0.01"),
        "volume_ratio20": Decimal("1.2"),
    }


class ProbabilityModelTests(unittest.TestCase):
    def test_temporal_split_is_global_and_embargoed(self) -> None:
        rows = []
        for day in range(400):
            for symbol in ("AAAUSDT", "BBBUSDT"):
                rows.append({
                    "symbol": symbol,
                    "candle_close_time": day * DAY_MS + DAY_MS - 1,
                    "features": {name: float(day) for name in FEATURE_NAMES},
                    "label": day % 2,
                })
        split = temporal_split(rows)
        self.assertLess(
            max(row["candle_close_time"] for row in split["train"]) + 5 * DAY_MS,
            min(row["candle_close_time"] for row in split["calibration"]),
        )
        self.assertLess(
            max(row["candle_close_time"] for row in split["calibration"]) + 5 * DAY_MS,
            min(row["candle_close_time"] for row in split["test"]),
        )
        # A timestamp is never divided by symbol across partitions.
        timestamp_sets = [{row["candle_close_time"] for row in split[name]} for name in ("train", "calibration", "test")]
        self.assertFalse(timestamp_sets[0] & timestamp_sets[1])
        self.assertFalse(timestamp_sets[1] & timestamp_sets[2])

    def test_pava_is_monotonic(self) -> None:
        blocks = pava([0.1, 0.2, 0.3, 0.4], [1, 0, 1, 0])
        calibrated = [block["probability"] for block in blocks]
        self.assertEqual(calibrated, sorted(calibrated))
        self.assertEqual(sum(block["count"] for block in blocks), 4)

    def test_pava_coalesces_equal_raw_scores_before_calibration(self) -> None:
        blocks = pava([0.2, 0.2, 0.8, 0.8], [1, 0, 1, 1])
        self.assertEqual(len(blocks), 2)
        self.assertEqual(blocks[0]["count"], 2)
        self.assertEqual(blocks[0]["probability"], 0.5)

    def test_calibration_does_not_jump_to_upper_block_inside_score_gap(self) -> None:
        blocks = [
            {"min_score": 0.1, "max_score": 0.2, "positive": 1, "count": 10, "probability": 0.1},
            {"min_score": 0.8, "max_score": 0.9, "positive": 9, "count": 10, "probability": 0.9},
        ]
        self.assertEqual(_calibrated(0.21, blocks)["probability"], 0.1)
        self.assertEqual(_calibrated(0.8, blocks)["probability"], 0.9)

    def test_insufficient_sample_has_no_invented_model(self) -> None:
        artifact = fit_artifact([], settings())
        self.assertEqual(artifact["status"], "INSUFFICIENT_SAMPLE")
        self.assertNotIn("model", artifact)
        self.assertNotIn("metrics", artifact)

    def test_artifact_is_canonical_hashed_and_tamper_evident(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            artifact = fit_artifact([], settings())
            write_artifact(path, artifact)
            self.assertEqual(path.read_text(encoding="utf-8"), canonical_json(artifact) + "\n")
            self.assertEqual(artifact["artifact_hash"], artifact_hash(artifact))
            tampered = dict(artifact)
            tampered["reason"] = "tampered"
            path.write_text(json.dumps(tampered), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_artifact(path, settings())


class ProbabilityPaperIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = AuditStore(Path(directory.name) / "paper.sqlite3")
        self.addCleanup(self.store.close)

    def _entry_with_features(self, timestamp: int):
        decision = entry_decision("AAAUSDT", "100", timestamp - 1)
        return replace(decision, metrics=feature_metrics())

    def test_required_blocks_entry_when_model_is_unavailable(self) -> None:
        config = replace(settings(), probability_gate_mode="REQUIRED")
        timestamp = 101 * DAY_MS
        payload = run_paper_cycle(
            report(timestamp, (self._entry_with_features(timestamp),), (bar("AAAUSDT", 100, "100", "101", "99", "100"),)),
            config,
            self.store,
        )
        self.assertEqual(payload["open_positions"], [])
        blocked = [event for event in payload["events_today"] if event["event_type"] == "ENTRY_BLOCKED_PROBABILITY"]
        self.assertEqual(blocked[0]["payload"]["probability_gate"]["status"], "UNAVAILABLE")
        self.assertEqual(self.store.probability_status()["collected"], 1)

    def test_collect_preserves_entry_and_records_gate_evidence(self) -> None:
        config = replace(settings(), probability_gate_mode="COLLECT")
        timestamp = 121 * DAY_MS
        payload = run_paper_cycle(
            report(timestamp, (self._entry_with_features(timestamp),), (bar("AAAUSDT", 120, "100", "101", "99", "100"),)),
            config,
            self.store,
        )
        self.assertEqual(len(payload["open_positions"]), 1)
        entry = [event for event in payload["events_today"] if event["event_type"] == "PAPER_ENTRY"][0]
        self.assertIn("probability_gate", entry["payload"])
        self.assertEqual(self.store.probability_status()["collected"], 1)

    def test_required_never_blocks_existing_position_exit(self) -> None:
        first = 141 * DAY_MS
        run_paper_cycle(
            report(first, (entry_decision("AAAUSDT", "100", first - 1),), (bar("AAAUSDT", 140, "100", "101", "99", "100"),)),
            settings(),
            self.store,
        )
        second = 142 * DAY_MS
        unavailable = replace(settings(), probability_gate_mode="REQUIRED")
        payload = run_paper_cycle(
            report(second, (no_entry_decision("AAAUSDT", "90", second - 1),), (bar("AAAUSDT", 141, "90", "91", "89", "90"),)),
            unavailable,
            self.store,
        )
        self.assertEqual(payload["open_positions"], [])
        self.assertIn("PAPER_EXIT", [event["event_type"] for event in payload["events_today"]])


if __name__ == "__main__":
    unittest.main()
