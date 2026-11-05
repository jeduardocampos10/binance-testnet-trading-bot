"""Closed-hour session statistics for report context, never a buy trigger."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as utc_timezone
from decimal import Decimal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Candle


@dataclass(frozen=True)
class HourProfile:
    local_hour: int
    utc_hours: tuple[int, ...]
    samples: int
    average_quote_volume: Decimal
    average_absolute_return_pct: Decimal
    risk_adjusted_return: Decimal
    current_spread_pct: Decimal
    score: Decimal

    def as_json_dict(self) -> dict[str, object]:
        text = lambda value: format(value, "f")
        return {
            "local_hour": self.local_hour,
            "utc_hours": list(self.utc_hours),
            "samples": self.samples,
            "average_quote_volume": text(self.average_quote_volume),
            "average_absolute_return_pct": text(self.average_absolute_return_pct),
            "risk_adjusted_return": text(self.risk_adjusted_return),
            "current_spread_pct": text(self.current_spread_pct),
            "score": text(self.score),
        }


@dataclass(frozen=True)
class AssetSessionProfile:
    symbol: str
    timezone: str
    favorable_hours: tuple[HourProfile, ...]

    def as_json_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "timezone": self.timezone,
            "favorable_hours": [item.as_json_dict() for item in self.favorable_hours],
        }


def _percentile_ranks(values: list[Decimal]) -> list[Decimal]:
    if len(values) == 1:
        return [Decimal("1")]
    ordered = sorted((value, index) for index, value in enumerate(values))
    result = [Decimal("0")] * len(values)
    position = 0
    while position < len(ordered):
        end = position + 1
        while end < len(ordered) and ordered[end][0] == ordered[position][0]:
            end += 1
        average_rank = Decimal(position + end - 1) / Decimal("2")
        percentile = average_rank / Decimal(len(values) - 1)
        for _, original_index in ordered[position:end]:
            result[original_index] = percentile
        position = end
    return result


def analyze_sessions(
    symbol: str,
    candles: list[Candle],
    *,
    spread_pct: Decimal,
    timezone: str,
    minimum_samples: int,
    top_hours: int = 3,
) -> AssetSessionProfile:
    """Rank local hours from closed hourly candles using descriptive statistics.

    The score is contextual only. It combines liquidity (35%), controlled
    volatility (25%), risk-adjusted return (30%), and sample coverage (10%).
    Current spread is reported as context and already
    gates universe eligibility; it cannot create an entry signal.
    """
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError:
        raise ValueError("unknown report timezone") from None
    if spread_pct < 0 or minimum_samples <= 0 or not 1 <= top_hours <= 24:
        raise ValueError("invalid session-analysis parameters")

    buckets: dict[int, list[tuple[int, Decimal, Decimal, Decimal]]] = {hour: [] for hour in range(24)}
    for candle in candles:
        local = datetime.fromtimestamp(candle.open_time / 1000, tz=utc_timezone.utc).astimezone(zone)
        return_pct = (candle.close / candle.open - Decimal("1")) * Decimal("100")
        quote_volume = candle.quote_volume if candle.quote_volume is not None else candle.volume * candle.close
        buckets[local.hour].append((local.astimezone(utc_timezone.utc).hour, quote_volume, abs(return_pct), return_pct))

    raw: list[tuple[int, tuple[int, ...], int, Decimal, Decimal, Decimal]] = []
    for hour, rows in buckets.items():
        if len(rows) < minimum_samples:
            continue
        count = Decimal(len(rows))
        average_volume = sum((row[1] for row in rows), Decimal("0")) / count
        average_volatility = sum((row[2] for row in rows), Decimal("0")) / count
        mean_return = sum((row[3] for row in rows), Decimal("0")) / count
        downside_squared = sum((min(row[3], Decimal("0")) ** 2 for row in rows), Decimal("0")) / count
        downside = downside_squared.sqrt() if downside_squared > 0 else Decimal("0")
        risk_adjusted = mean_return / downside if downside > 0 else (Decimal("1") if mean_return > 0 else Decimal("0"))
        utc_hours = tuple(sorted({row[0] for row in rows}))
        raw.append((hour, utc_hours, len(rows), average_volume, average_volatility, risk_adjusted))

    if not raw:
        return AssetSessionProfile(symbol=symbol, timezone=timezone, favorable_hours=())

    liquidity = _percentile_ranks([row[3] for row in raw])
    risk_adjusted = _percentile_ranks([row[5] for row in raw])
    samples = _percentile_ranks([Decimal(row[2]) for row in raw])
    volatility_values = [row[4] for row in raw]
    median = sorted(volatility_values)[len(volatility_values) // 2]
    controlled_volatility = _percentile_ranks([-abs(value - median) for value in volatility_values])
    profiles = []
    for index, row in enumerate(raw):
        score = (
            Decimal("0.35") * liquidity[index]
            + Decimal("0.25") * controlled_volatility[index]
            + Decimal("0.30") * risk_adjusted[index]
            + Decimal("0.10") * samples[index]
        )
        profiles.append(HourProfile(row[0], row[1], row[2], row[3], row[4], row[5], spread_pct, score))
    profiles.sort(key=lambda item: (item.score, -item.local_hour), reverse=True)
    return AssetSessionProfile(symbol=symbol, timezone=timezone, favorable_hours=tuple(profiles[:top_hours]))
