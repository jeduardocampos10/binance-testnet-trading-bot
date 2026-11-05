from __future__ import annotations

import unittest
from decimal import Decimal

from app.ranking import CandidateSnapshot, rank_assets
from tests.helpers import candles_from_closes, settings


class RankingTests(unittest.TestCase):
    def test_ranks_momentum_without_making_every_rank_eligible(self) -> None:
        rising = candles_from_closes([Decimal(100 + index) for index in range(90)])
        flat = candles_from_closes([Decimal("100")] * 90)
        snapshots = [
            CandidateSnapshot("AAAUSDT", tuple(rising), Decimal("1000000"), Decimal("0.1"), Decimal("5")),
            CandidateSnapshot("BBBUSDT", tuple(flat), Decimal("500000"), Decimal("0.2"), Decimal("5")),
        ]
        result = rank_assets(snapshots, settings())
        self.assertEqual(result[0].symbol, "AAAUSDT")
        self.assertTrue(result[0].entry_eligible)
        flat_result = next(item for item in result if item.symbol == "BBBUSDT")
        self.assertFalse(flat_result.entry_eligible)

    def test_excludes_insufficient_history(self) -> None:
        snapshot = CandidateSnapshot(
            "AAAUSDT",
            tuple(candles_from_closes([Decimal("100")] * 20)),
            Decimal("1000"),
            Decimal("0.1"),
            Decimal("5"),
        )
        self.assertEqual(rank_assets([snapshot], settings()), [])


if __name__ == "__main__":
    unittest.main()
