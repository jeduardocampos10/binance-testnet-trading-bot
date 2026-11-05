"""Domain models and strict candle validation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


class DataValidationError(ValueError):
    """Raised when market data is unsafe to analyze."""


@dataclass(frozen=True)
class Candle:
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal | None = None

    def __post_init__(self) -> None:
        if self.open_time < 0 or self.close_time <= self.open_time:
            raise DataValidationError("invalid candle timestamps")
        values = (self.open, self.high, self.low, self.close, self.volume)
        if not all(value.is_finite() for value in values) or (
            self.quote_volume is not None and not self.quote_volume.is_finite()
        ):
            raise DataValidationError("candle values must be finite")
        if any(value <= 0 for value in (self.open, self.high, self.low, self.close)):
            raise DataValidationError("candle prices must be positive")
        if self.volume < 0 or (self.quote_volume is not None and self.quote_volume < 0):
            raise DataValidationError("candle volume cannot be negative")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise DataValidationError("inconsistent OHLC values")
        if self.low > self.high:
            raise DataValidationError("candle low exceeds high")


def _validate_candles(candles: Iterable[Candle], *, now_ms: int, interval_ms: int, label: str) -> list[Candle]:
    result = list(candles)
    if not result:
        raise DataValidationError("no candles received")
    for index, candle in enumerate(result):
        if candle.close_time >= now_ms:
            raise DataValidationError("open candle must not be analyzed")
        if candle.close_time != candle.open_time + interval_ms - 1:
            raise DataValidationError(f"invalid {label} candle duration")
        if index:
            previous = result[index - 1]
            if candle.open_time <= previous.open_time:
                raise DataValidationError("candles are duplicated or out of order")
            if candle.open_time - previous.open_time != interval_ms:
                raise DataValidationError(f"{label} candle sequence has a gap")
    expected_latest_open = (now_ms // interval_ms) * interval_ms - interval_ms
    if result[-1].open_time != expected_latest_open:
        raise DataValidationError(f"latest closed {label} candle is stale or misaligned")
    return result


def validate_daily_candles(candles: Iterable[Candle], *, now_ms: int) -> list[Candle]:
    """Return ordered, closed, contiguous daily candles or fail closed."""
    return _validate_candles(candles, now_ms=now_ms, interval_ms=DAY_MS, label="daily")


def validate_hourly_candles(candles: Iterable[Candle], *, now_ms: int) -> list[Candle]:
    """Return ordered, closed, contiguous hourly candles or fail closed."""
    return _validate_candles(candles, now_ms=now_ms, interval_ms=HOUR_MS, label="hourly")
