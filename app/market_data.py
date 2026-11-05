"""Strict unauthenticated clients for two fixed Binance public-data hosts."""

from __future__ import annotations

import json
import re
import socket
import ssl
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, OpenerDirector, ProxyHandler, Request, build_opener

from .models import Candle, DataValidationError
from .universe_policy import EFFECTIVE_MANDATORY_EXCLUDED_BASE_ASSETS

DATA_SOURCES = {
    "BINANCE_SPOT_TESTNET": "https://testnet.binance.vision",
    "BINANCE_PUBLIC_PRODUCTION": "https://data-api.binance.vision",
}
TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SYMBOL_RE = re.compile(r"^[A-Z0-9]{5,24}$")


class MarketDataError(Exception):
    """Safe public-data failure."""


@dataclass(frozen=True)
class UniverseSymbol:
    symbol: str
    base_asset: str
    quote_asset: str
    min_notional: Decimal


class RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        raise MarketDataError(f"market-data redirect rejected (status {code})")


def build_direct_opener() -> OpenerDirector:
    return build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        RejectRedirects(),
    )


class MarketDataClient:
    def __init__(self, source: str = "BINANCE_SPOT_TESTNET", *, opener: OpenerDirector | None = None) -> None:
        if source not in DATA_SOURCES:
            raise MarketDataError("market-data source is not allowlisted")
        self.source = source
        self._base_url = DATA_SOURCES[source]
        self._expected_host = urlsplit(self._base_url).netloc
        self._opener = opener if opener is not None else build_direct_opener()

    def _get_json(self, path: str, params: Mapping[str, str] | None = None) -> object:
        if path not in {"/api/v3/time", "/api/v3/exchangeInfo", "/api/v3/ticker/24hr", "/api/v3/ticker/bookTicker", "/api/v3/klines"}:
            raise MarketDataError("market-data path is not allowlisted")
        query = urlencode(params or {})
        expected_url = f"{self._base_url}{path}" + (f"?{query}" if query else "")
        request = Request(
            expected_url,
            headers={"Accept": "application/json", "User-Agent": "cashmaxx-public-market-observer/0.3"},
            method="GET",
        )
        try:
            with self._opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise MarketDataError(f"market data returned HTTP status {response.status}")
                if response.geturl() != expected_url:
                    raise MarketDataError("unexpected final market-data URL")
                parts = urlsplit(response.geturl())
                if (
                    parts.scheme != "https"
                    or parts.netloc != self._expected_host
                    or parts.path != path
                    or parts.fragment
                ):
                    raise MarketDataError("unexpected final market-data URL")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except MarketDataError:
            raise
        except HTTPError as exc:
            raise MarketDataError(f"market-data request failed with HTTP status {exc.code}") from None
        except (URLError, socket.timeout, TimeoutError):
            raise MarketDataError("market-data network request failed") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MarketDataError("market-data response exceeded size limit")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise MarketDataError("market data returned invalid JSON") from None

    def server_time_ms(self) -> int:
        payload = self._get_json("/api/v3/time")
        if not isinstance(payload, dict):
            raise MarketDataError("server time has unexpected structure")
        value = payload.get("serverTime")
        if not isinstance(value, int) or value <= 0:
            raise MarketDataError("server time is invalid")
        return value

    def eligible_symbols(
        self,
        quote_asset: str,
        maximum_position_usdt: Decimal,
        excluded_base_assets: tuple[str, ...] = (),
    ) -> list[UniverseSymbol]:
        if quote_asset != "USDT" or maximum_position_usdt <= 0:
            raise MarketDataError("invalid universe request")
        payload = self._get_json("/api/v3/exchangeInfo")
        if not isinstance(payload, dict) or not isinstance(payload.get("symbols"), list):
            raise MarketDataError("exchange information has unexpected structure")
        excluded = EFFECTIVE_MANDATORY_EXCLUDED_BASE_ASSETS | set(excluded_base_assets)
        result: list[UniverseSymbol] = []
        for item in payload["symbols"]:
            if not isinstance(item, dict):
                continue
            symbol = item.get("symbol")
            base = item.get("baseAsset")
            quote = item.get("quoteAsset")
            if (
                not isinstance(symbol, str)
                or not SYMBOL_RE.fullmatch(symbol)
                or not isinstance(base, str)
                or not isinstance(quote, str)
                or quote != quote_asset
                or item.get("status") != "TRADING"
                or item.get("isSpotTradingAllowed") is not True
                or base in excluded
            ):
                continue
            filters = item.get("filters")
            if not isinstance(filters, list):
                continue
            min_notional: Decimal | None = None
            for filter_item in filters:
                if not isinstance(filter_item, dict):
                    continue
                if filter_item.get("filterType") in {"NOTIONAL", "MIN_NOTIONAL"}:
                    raw_minimum = filter_item.get("minNotional")
                    if isinstance(raw_minimum, str):
                        try:
                            candidate = Decimal(raw_minimum)
                        except InvalidOperation:
                            continue
                        if candidate.is_finite() and candidate >= 0:
                            min_notional = candidate
                            break
            if min_notional is None or min_notional > maximum_position_usdt:
                continue
            result.append(UniverseSymbol(symbol, base, quote, min_notional))
        if not result:
            raise MarketDataError("no eligible symbols found")
        return result

    def quote_volumes_24h(self) -> dict[str, Decimal]:
        payload = self._get_json("/api/v3/ticker/24hr")
        if not isinstance(payload, list):
            raise MarketDataError("24h tickers have unexpected structure")
        result: dict[str, Decimal] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = item.get("symbol")
            raw_volume = item.get("quoteVolume")
            if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol) or not isinstance(raw_volume, str):
                continue
            try:
                volume = Decimal(raw_volume)
            except InvalidOperation:
                continue
            if volume.is_finite() and volume >= 0:
                result[symbol] = volume
        return result

    def spreads_pct(self) -> dict[str, Decimal]:
        payload = self._get_json("/api/v3/ticker/bookTicker")
        if not isinstance(payload, list):
            raise MarketDataError("book tickers have unexpected structure")
        result: dict[str, Decimal] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            symbol = item.get("symbol")
            bid_raw = item.get("bidPrice")
            ask_raw = item.get("askPrice")
            if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
                continue
            if not isinstance(bid_raw, str) or not isinstance(ask_raw, str):
                continue
            try:
                bid, ask = Decimal(bid_raw), Decimal(ask_raw)
            except InvalidOperation:
                continue
            midpoint = (bid + ask) / Decimal("2")
            if bid > 0 and ask >= bid and midpoint > 0:
                result[symbol] = (ask - bid) / midpoint * Decimal("100")
        return result

    def daily_candles(self, symbol: str, limit: int) -> list[Candle]:
        if not SYMBOL_RE.fullmatch(symbol):
            raise MarketDataError("invalid symbol")
        if not 90 <= limit <= 1000:
            raise MarketDataError("invalid candle limit")
        payload = self._get_json(
            "/api/v3/klines",
            {"symbol": symbol, "interval": "1d", "limit": str(limit)},
        )
        return self._parse_klines(payload)

    def hourly_candles(self, symbol: str, limit: int) -> list[Candle]:
        if not SYMBOL_RE.fullmatch(symbol):
            raise MarketDataError("invalid symbol")
        if not 480 <= limit <= 1000:
            raise MarketDataError("invalid hourly candle limit")
        payload = self._get_json(
            "/api/v3/klines",
            {"symbol": symbol, "interval": "1h", "limit": str(limit)},
        )
        return self._parse_klines(payload)

    @staticmethod
    def _parse_klines(payload: object) -> list[Candle]:
        if not isinstance(payload, list):
            raise MarketDataError("market data has unexpected structure")
        candles: list[Candle] = []
        try:
            for item in payload:
                if not isinstance(item, list) or len(item) < 7:
                    raise MarketDataError("market data contains an invalid candle")
                if not isinstance(item[0], int) or not isinstance(item[6], int):
                    raise MarketDataError("market data contains invalid timestamps")
                if not all(isinstance(item[index], str) for index in (1, 2, 3, 4, 5)):
                    raise MarketDataError("market data contains invalid decimal fields")
                quote_volume = None
                if len(item) > 7:
                    if not isinstance(item[7], str):
                        raise MarketDataError("market data contains invalid quote volume")
                    quote_volume = Decimal(item[7])
                candles.append(
                    Candle(
                        open_time=item[0],
                        close_time=item[6],
                        open=Decimal(item[1]),
                        high=Decimal(item[2]),
                        low=Decimal(item[3]),
                        close=Decimal(item[4]),
                        volume=Decimal(item[5]),
                        quote_volume=quote_volume,
                    )
                )
        except (InvalidOperation, DataValidationError):
            raise MarketDataError("market data contains an invalid candle") from None
        return candles
