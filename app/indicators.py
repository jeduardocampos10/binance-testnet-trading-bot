"""Small deterministic technical-indicator implementation using Decimal."""

from __future__ import annotations

from decimal import Decimal

from .models import Candle


def sma(values: list[Decimal], period: int) -> Decimal:
    if period <= 0 or len(values) < period:
        raise ValueError("insufficient values for SMA")
    return sum(values[-period:], Decimal("0")) / Decimal(period)


def ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    if period <= 0 or len(values) < period:
        raise ValueError("insufficient values for EMA")
    seed = sum(values[:period], Decimal("0")) / Decimal(period)
    multiplier = Decimal("2") / Decimal(period + 1)
    result = [seed]
    for value in values[period:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def rsi(values: list[Decimal], period: int = 14) -> Decimal:
    if period <= 0 or len(values) < period + 1:
        raise ValueError("insufficient values for RSI")
    changes = [values[index] - values[index - 1] for index in range(len(values) - period, len(values))]
    gains = sum((max(change, Decimal("0")) for change in changes), Decimal("0")) / Decimal(period)
    losses = sum((max(-change, Decimal("0")) for change in changes), Decimal("0")) / Decimal(period)
    if losses == 0:
        return Decimal("100") if gains > 0 else Decimal("50")
    relative_strength = gains / losses
    return Decimal("100") - Decimal("100") / (Decimal("1") + relative_strength)


def atr(candles: list[Candle], period: int = 14) -> Decimal:
    if period <= 0 or len(candles) < period + 1:
        raise ValueError("insufficient candles for ATR")
    true_ranges: list[Decimal] = []
    for index in range(len(candles) - period, len(candles)):
        candle = candles[index]
        previous_close = candles[index - 1].close
        true_ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    return sum(true_ranges, Decimal("0")) / Decimal(period)
