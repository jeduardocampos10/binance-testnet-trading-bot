from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from app.config import ConfigurationError, load_settings
from app.universe_policy import MANDATORY_EXCLUDED_BASE_ASSETS


VALID = {
    "quote_asset": "USDT",
    "strategy_version": "monthly-top20-trend-cross-v0.5-crypto-only",
    "market_data_source": "BINANCE_PUBLIC_PRODUCTION",
    "future_execution_target": "BINANCE_SPOT_TESTNET",
    "excluded_base_assets": sorted(MANDATORY_EXCLUDED_BASE_ASSETS),
    "top_n": 20,
    "prefilter_n": 60,
    "history_limit": 120,
    "minimum_history": 90,
    "paper_equity_usdt": "100",
    "max_positions": 3,
    "max_exposure_pct": "10",
    "hard_stop_pct": "5",
    "trailing_stop_pct": "3",
    "daily_loss_limit_pct": "3",
    "reward_risk_ratio": "2",
    "daily_goal_pct": "1",
    "fee_per_side_pct": "0.1",
    "slippage_per_side_pct": "0.05",
    "rsi_min": "50",
    "rsi_max": "68",
    "minimum_volume_ratio": "0.8",
    "maximum_spread_pct": "0.5",
    "hourly_history_limit": 1000,
    "minimum_hour_samples": 20,
    "report_timezone": "America/Sao_Paulo",
}


class ConfigurationTests(unittest.TestCase):
    def _write(self, payload: dict[str, object]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_loads_safe_defaults(self) -> None:
        result = load_settings(self._write(VALID))
        self.assertEqual(result.quote_asset, "USDT")
        self.assertEqual(str(result.paper_equity_usdt), "100")
        self.assertEqual(result.max_positions, 3)

    def test_rejects_non_allowlisted_quote_asset(self) -> None:
        payload = dict(VALID)
        payload["quote_asset"] = "BTC"
        with self.assertRaises(ConfigurationError):
            load_settings(self._write(payload))

    def test_rejects_excessive_daily_loss(self) -> None:
        payload = dict(VALID)
        payload["daily_loss_limit_pct"] = "6"
        with self.assertRaises(ConfigurationError):
            load_settings(self._write(payload))

    def test_rejects_non_public_market_data_source(self) -> None:
        payload = dict(VALID)
        payload["market_data_source"] = "BINANCE_SPOT_TESTNET"
        with self.assertRaises(ConfigurationError):
            load_settings(self._write(payload))

    def test_rejects_non_testnet_future_target(self) -> None:
        payload = dict(VALID)
        payload["future_execution_target"] = "BINANCE_SPOT"
        with self.assertRaises(ConfigurationError):
            load_settings(self._write(payload))

    def test_rejects_removal_of_mandatory_crypto_only_exclusion(self) -> None:
        payload = dict(VALID)
        payload["excluded_base_assets"] = [
            item for item in VALID["excluded_base_assets"] if item != "USD1"
        ]
        with self.assertRaises(ConfigurationError):
            load_settings(self._write(payload))


if __name__ == "__main__":
    unittest.main()
