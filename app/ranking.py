"""Monthly top-20 universe ranking with liquidity and risk controls."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .config import Settings
from .indicators import sma
from .models import Candle


@dataclass(frozen=True)
class CandidateSnapshot:
    symbol: str
    candles: tuple[Candle, ...]
    quote_volume_24h: Decimal
    spread_pct: Decimal
    min_notional: Decimal


@dataclass(frozen=True)
class RankedAsset:
    rank: int
    symbol: str
    score: Decimal
    month_return_pct: Decimal
    return_30d_pct: Decimal
    maximum_drawdown_30d_pct: Decimal
    quote_volume_24h: Decimal
    spread_pct: Decimal
    trend_eligible: bool
    entry_eligible: bool
    eligibility_reasons: tuple[str, ...]

    def as_json_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "score": format(self.score, "f"),
            "month_return_pct": format(self.month_return_pct, "f"),
            "return_30d_pct": format(self.return_30d_pct, "f"),
            "maximum_drawdown_30d_pct": format(self.maximum_drawdown_30d_pct, "f"),
            "quote_volume_24h": format(self.quote_volume_24h, "f"),
            "spread_pct": format(self.spread_pct, "f"),
            "trend_eligible": self.trend_eligible,
            "entry_eligible": self.entry_eligible,
            "eligibility_reasons": list(self.eligibility_reasons),
        }


@dataclass(frozen=True)
class _Metrics:
    snapshot: CandidateSnapshot
    month_return: Decimal
    return_30d: Decimal
    drawdown_30d: Decimal
    trend_eligible: bool
    reasons: tuple[str, ...]


def _return_pct(start: Decimal, end: Decimal) -> Decimal:
    return (end / start - Decimal("1")) * Decimal("100")


def _maximum_drawdown_pct(candles: tuple[Candle, ...]) -> Decimal:
    peak = candles[0].close
    maximum = Decimal("0")
    for candle in candles:
        peak = max(peak, candle.close)
        drawdown = (peak - candle.close) / peak * Decimal("100")
        maximum = max(maximum, drawdown)
    return maximum


def _percentiles(values: dict[str, Decimal], *, ascending_is_better: bool = False) -> dict[str, Decimal]:
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]), reverse=not ascending_is_better)
    count = len(ordered)
    if count == 1:
        return {ordered[0][0]: Decimal("1")}
    # Best value receives 1 and worst receives 0. Ties remain deterministic;
    # ranking is observational and never bypasses the entry rules.
    denominator = Decimal(count - 1)
    return {symbol: Decimal(count - 1 - index) / denominator for index, (symbol, _) in enumerate(ordered)}


def _metrics(snapshot: CandidateSnapshot, settings: Settings) -> _Metrics:
    candles = snapshot.candles
    closes = [candle.close for candle in candles]
    latest_date = datetime.fromtimestamp(candles[-1].open_time / 1000, tz=UTC)
    month_candles = [
        candle
        for candle in candles
        if (date := datetime.fromtimestamp(candle.open_time / 1000, tz=UTC)).year == latest_date.year
        and date.month == latest_date.month
    ]
    month_start = month_candles[0].open if month_candles else candles[-30].open
    month_return = _return_pct(month_start, candles[-1].close)
    return_30d = _return_pct(candles[-30].open, candles[-1].close)
    drawdown = _maximum_drawdown_pct(candles[-30:])
    current_sma30 = sma(closes, 30)
    current_sma60 = sma(closes, 60)
    trend_eligible = candles[-1].close > current_sma60 and current_sma30 > current_sma60

    reasons: list[str] = []
    if month_return <= 0:
        reasons.append("month-to-date return is not positive")
    if return_30d <= 0:
        reasons.append("30-day return is not positive")
    if not trend_eligible:
        reasons.append("30/60-day trend filter failed")
    if snapshot.spread_pct > settings.maximum_spread_pct:
        reasons.append("spread exceeds configured maximum")
    maximum_position = settings.paper_equity_usdt * settings.max_exposure_pct / Decimal("100")
    if snapshot.min_notional > maximum_position:
        reasons.append("minimum notional exceeds position budget")
    return _Metrics(snapshot, month_return, return_30d, drawdown, trend_eligible, tuple(reasons))


def rank_assets(snapshots: list[CandidateSnapshot], settings: Settings) -> list[RankedAsset]:
    valid = [snapshot for snapshot in snapshots if len(snapshot.candles) >= settings.minimum_history]
    if not valid:
        return []
    metrics = [_metrics(snapshot, settings) for snapshot in valid]
    month_scores = _percentiles({item.snapshot.symbol: item.month_return for item in metrics})
    return_scores = _percentiles({item.snapshot.symbol: item.return_30d for item in metrics})
    liquidity_scores = _percentiles({item.snapshot.symbol: item.snapshot.quote_volume_24h for item in metrics})
    spread_scores = _percentiles(
        {item.snapshot.symbol: item.snapshot.spread_pct for item in metrics},
        ascending_is_better=True,
    )
    drawdown_scores = _percentiles(
        {item.snapshot.symbol: item.drawdown_30d for item in metrics},
        ascending_is_better=True,
    )

    scored: list[tuple[Decimal, _Metrics]] = []
    for item in metrics:
        symbol = item.snapshot.symbol
        score = (
            month_scores[symbol] * Decimal("0.30")
            + return_scores[symbol] * Decimal("0.20")
            + liquidity_scores[symbol] * Decimal("0.20")
            + spread_scores[symbol] * Decimal("0.15")
            + drawdown_scores[symbol] * Decimal("0.15")
        )
        scored.append((score, item))
    scored.sort(key=lambda pair: (pair[0], pair[1].snapshot.symbol), reverse=True)

    ranked: list[RankedAsset] = []
    for index, (score, item) in enumerate(scored[: settings.top_n], start=1):
        ranked.append(
            RankedAsset(
                rank=index,
                symbol=item.snapshot.symbol,
                score=score,
                month_return_pct=item.month_return,
                return_30d_pct=item.return_30d,
                maximum_drawdown_30d_pct=item.drawdown_30d,
                quote_volume_24h=item.snapshot.quote_volume_24h,
                spread_pct=item.snapshot.spread_pct,
                trend_eligible=item.trend_eligible,
                entry_eligible=not item.reasons,
                eligibility_reasons=item.reasons,
            )
        )
    return ranked
