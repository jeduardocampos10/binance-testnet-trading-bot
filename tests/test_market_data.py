from __future__ import annotations

import unittest

from app.market_data import MarketDataClient, MarketDataError
from app.universe_policy import KNOWN_TOKENIZED_SECURITY_BASE_ASSETS


class FakeResponse:
    status = 200

    def __init__(self, url: str) -> None:
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def geturl(self) -> str:
        return self._url

    def read(self, size: int) -> bytes:
        return b'{"serverTime":123}'


class RecordingOpener:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.methods: list[str] = []

    def open(self, request, timeout: int):
        self.urls.append(request.full_url)
        self.methods.append(request.get_method())
        return FakeResponse(request.full_url)


class MarketDataParsingTests(unittest.TestCase):
    def test_parses_kline(self) -> None:
        payload = [[0, "100", "102", "99", "101", "12.5", 86_399_999, "0", 1, "0", "0", "0"]]
        result = MarketDataClient._parse_klines(payload)
        self.assertEqual(str(result[0].close), "101")

    def test_rejects_bad_structure(self) -> None:
        with self.assertRaises(MarketDataError):
            MarketDataClient._parse_klines({"not": "klines"})

    def test_rejects_malformed_symbol_before_network(self) -> None:
        with self.assertRaises(MarketDataError):
            MarketDataClient().daily_candles("../ETHUSDT", 120)

    def test_sources_are_fixed_and_get_only(self) -> None:
        cases = {
            "BINANCE_SPOT_TESTNET": "https://testnet.binance.vision/api/v3/time",
            "BINANCE_PUBLIC_PRODUCTION": "https://data-api.binance.vision/api/v3/time",
        }
        for source, expected_url in cases.items():
            opener = RecordingOpener()
            client = MarketDataClient(source, opener=opener)
            self.assertEqual(client.server_time_ms(), 123)
            self.assertEqual(opener.urls, [expected_url])
            self.assertEqual(opener.methods, ["GET"])

    def test_rejects_arbitrary_market_data_source(self) -> None:
        with self.assertRaises(MarketDataError):
            MarketDataClient("https://evil.example")


if __name__ == "__main__":
    unittest.main()


class ExchangePayloadClient(MarketDataClient):
    def __init__(self, payload):
        super().__init__()
        self.payload = payload

    def _get_json(self, path, params=None):
        return self.payload


class CryptoOnlyUniverseTests(unittest.TestCase):
    def test_excludes_stable_gold_and_tokenized_equity_assets(self) -> None:
        symbols = []
        bases = {
            "BTC", "JUP", "BTCUP", "USD1", "RLUSD", "BFUSD", "XAUT", "PAXG", "SPCX",
            *KNOWN_TOKENIZED_SECURITY_BASE_ASSETS,
        }
        for base in sorted(bases):
            symbols.append(
                {
                    "symbol": f"{base}USDT",
                    "baseAsset": base,
                    "quoteAsset": "USDT",
                    "status": "TRADING",
                    "isSpotTradingAllowed": True,
                    "filters": [{"filterType": "MIN_NOTIONAL", "minNotional": "5"}],
                }
            )
        result = ExchangePayloadClient({"symbols": symbols}).eligible_symbols("USDT", __import__("decimal").Decimal("10"))
        self.assertEqual([item.symbol for item in result], ["BTCUSDT", "JUPUSDT"])
