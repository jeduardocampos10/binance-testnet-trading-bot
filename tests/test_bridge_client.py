from __future__ import annotations

import http.client
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.bridge_client import (
    BridgeBalanceClient,
    BridgeUnavailable,
    read_bridge_token,
    validate_bridge_base_url,
)


class _Response:
    status = 200

    def __init__(self, payload: object, url: str) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, _limit: int) -> bytes:
        return self._raw


class _Opener:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls = 0
        self.authorization = None

    def open(self, request, timeout: int):
        self.calls += 1
        self.authorization = request.get_header("Authorization")
        self.timeout = timeout
        return _Response(self.payload, request.full_url)


class BridgeBalanceClientTests(unittest.TestCase):
    def test_only_approved_private_or_loopback_urls_are_accepted(self) -> None:
        self.assertEqual(validate_bridge_base_url("http://binance-testnet-bridge:8080"),
                         "http://binance-testnet-bridge:8080")
        with self.assertRaises(ValueError):
            validate_bridge_base_url("https://example.com")
        with self.assertRaises(ValueError):
            validate_bridge_base_url("http://localhost:8080/path")
        with self.assertRaises(ValueError):
            validate_bridge_base_url("http://user:pass@localhost:8080")

    def test_balance_normalizes_contract_and_caches_without_order_capability(self) -> None:
        opener = _Opener({
            "environment": "binance-spot-testnet",
            "balances": [{"asset": "USDT", "free": "1000.0", "locked": "0"}],
        })
        clock_value = [100.0]
        client = BridgeBalanceClient(
            "http://localhost:8080", "t" * 32, opener=opener,
            clock=lambda: clock_value[0], cache_ttl_seconds=15,
        )
        first = client.balance()
        second = client.balance()
        self.assertEqual(opener.calls, 1)
        self.assertEqual(opener.authorization, "Bearer " + "t" * 32)
        self.assertEqual(first["environment"], "binance-spot-testnet")
        self.assertEqual(first["observed_at_ms"], 100000)
        self.assertTrue(first["read_only"])
        self.assertFalse(first["orders_enabled"])
        self.assertEqual(second["status"], "fresh")

    def test_cache_expiry_uses_monotonic_time_when_wall_clock_moves_backwards(self) -> None:
        opener = _Opener({"environment": "binance-spot-testnet", "balances": []})
        wall = [100.0]
        monotonic = [10.0]
        client = BridgeBalanceClient(
            "http://localhost:8080", "t" * 32, opener=opener,
            clock=lambda: wall[0], monotonic_clock=lambda: monotonic[0],
            cache_ttl_seconds=15, max_stale_seconds=30,
        )
        client.balance()
        wall[0] = 1.0
        monotonic[0] = 26.0
        stale = client.balance()
        self.assertEqual(stale["status"], "fresh")
        self.assertEqual(opener.calls, 2)
        self.assertEqual(stale["fetched_at_ms"], 1000)

    def test_http_protocol_and_deep_json_failures_are_sanitized(self) -> None:
        class BrokenOpener:
            def open(self, _request, timeout: int):
                raise http.client.IncompleteRead(b"partial")

        client = BridgeBalanceClient("http://localhost:8080", "t" * 32, opener=BrokenOpener())
        with self.assertRaisesRegex(BridgeUnavailable, "temporarily unavailable"):
            client.balance()
        with patch("app.bridge_client.json.loads", side_effect=RecursionError):
            client = BridgeBalanceClient(
                "http://localhost:8080", "t" * 32,
                opener=_Opener({"environment": "binance-spot-testnet", "balances": []}),
            )
            with self.assertRaisesRegex(BridgeUnavailable, "invalid JSON"):
                client.balance()

    def test_invalid_or_negative_balance_fails_closed(self) -> None:
        opener = _Opener({
            "environment": "binance-spot-testnet",
            "balances": [{"asset": "USDT", "free": "-1", "locked": "0"}],
        })
        client = BridgeBalanceClient("http://localhost:8080", "t" * 32, opener=opener)
        with self.assertRaises(BridgeUnavailable):
            client.balance()

    def test_token_file_must_be_regular_and_long(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            path.write_text("short", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_bridge_token(path)
            path.write_text("z" * 32 + "\n", encoding="utf-8")
            self.assertEqual(read_bridge_token(path), "z" * 32)


if __name__ == "__main__":
    unittest.main()
