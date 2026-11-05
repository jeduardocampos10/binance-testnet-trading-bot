"""One-shot top-20 observation workflow; never sends authenticated requests."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .config import Settings
from .market_data import MarketDataClient, MarketDataError, UniverseSymbol
from .models import DataValidationError, validate_daily_candles, validate_hourly_candles
from .ranking import CandidateSnapshot, RankedAsset, rank_assets
from .sessions import AssetSessionProfile, analyze_sessions
from .strategy import Decision, evaluate


@dataclass(frozen=True)
class MarketBar:
    symbol: str
    open_time: int
    close_time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.open_time < 0 or self.close_time <= self.open_time:
            raise DataValidationError("invalid market-bar timestamps")
        values = (self.open, self.high, self.low, self.close)
        if not all(value.is_finite() and value > 0 for value in values):
            raise DataValidationError("market-bar prices must be finite and positive")
        if self.low > self.high or self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise DataValidationError("inconsistent market-bar OHLC values")

    def as_json_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "open_time": self.open_time,
            "close_time": self.close_time,
            "open": format(self.open, "f"),
            "high": format(self.high, "f"),
            "low": format(self.low, "f"),
            "close": format(self.close, "f"),
        }


@dataclass(frozen=True)
class ObservationReport:
    generated_at_ms: int
    mode: str
    strategy_version: str
    market_data_source: str
    future_execution_target: str
    universe_count: int
    prefiltered_count: int
    analyzed_count: int
    skipped_symbols: tuple[str, ...]
    skipped_reasons: dict[str, str]
    ranking: tuple[RankedAsset, ...]
    decisions: tuple[Decision, ...]
    market_bars: tuple[MarketBar, ...]
    session_profiles: tuple[AssetSessionProfile, ...]
    session_skipped_symbols: tuple[str, ...]

    def as_json_dict(self) -> dict[str, object]:
        return {
            "generated_at_ms": self.generated_at_ms,
            "mode": self.mode,
            "strategy_version": self.strategy_version,
            "market_data_source": self.market_data_source,
            "future_execution_target": self.future_execution_target,
            "execution_mode": "PAPER_DISABLED_IN_OBSERVATION",
            "universe_count": self.universe_count,
            "prefiltered_count": self.prefiltered_count,
            "analyzed_count": self.analyzed_count,
            "skipped_symbols": list(self.skipped_symbols),
            "skipped_reasons": dict(sorted(self.skipped_reasons.items())),
            "ranking": [asset.as_json_dict() for asset in self.ranking],
            "decisions": [decision.as_json_dict() for decision in self.decisions],
            "market_bars": [bar.as_json_dict() for bar in self.market_bars],
            "session_profiles": [profile.as_json_dict() for profile in self.session_profiles],
            "session_skipped_symbols": list(self.session_skipped_symbols),
        }


def observe_market(
    settings: Settings,
    *,
    client: MarketDataClient | None = None,
    testnet_client: MarketDataClient | None = None,
    now_ms: int | None = None,
    required_symbols: tuple[str, ...] = (),
) -> ObservationReport:
    market = client if client is not None else MarketDataClient(settings.market_data_source)
    # Tests may intentionally inject one fake for both roles. Production always
    # keeps the Testnet eligibility client separate from production market data.
    eligibility = testnet_client if testnet_client is not None else (
        client if client is not None else MarketDataClient("BINANCE_SPOT_TESTNET")
    )
    timestamp = now_ms if now_ms is not None else market.server_time_ms()
    maximum_position = settings.paper_equity_usdt * settings.max_exposure_pct / Decimal("100")
    universe = eligibility.eligible_symbols(
        settings.quote_asset, maximum_position, settings.excluded_base_assets
    )
    volumes = market.quote_volumes_24h()
    spreads = market.spreads_pct()

    prefiltered = [
        item
        for item in universe
        if item.symbol in volumes
        and item.symbol in spreads
        and spreads[item.symbol] <= settings.maximum_spread_pct
    ]
    prefiltered.sort(key=lambda item: (volumes[item.symbol], item.symbol), reverse=True)
    prefiltered = prefiltered[: settings.prefilter_n]
    ranking_symbols = {item.symbol for item in prefiltered}
    requested = set(required_symbols)
    analysis_items = list(prefiltered)

    snapshots: list[CandidateSnapshot] = []
    skipped: list[str] = []
    skipped_reasons: dict[str, str] = {}
    for item in analysis_items:
        try:
            raw = market.daily_candles(item.symbol, settings.history_limit)
            closed = [candle for candle in raw if candle.close_time < timestamp]
            candles = validate_daily_candles(closed, now_ms=timestamp)
            if len(candles) < settings.minimum_history:
                raise DataValidationError("insufficient history")
        except (MarketDataError, DataValidationError) as exc:
            skipped.append(item.symbol)
            skipped_reasons[item.symbol] = str(exc)
            continue
        snapshots.append(
            CandidateSnapshot(
                symbol=item.symbol,
                candles=tuple(candles),
                quote_volume_24h=volumes[item.symbol],
                spread_pct=spreads[item.symbol],
                min_notional=item.min_notional,
            )
        )

    ranking = rank_assets(snapshots, settings)
    candles_by_symbol = {snapshot.symbol: list(snapshot.candles) for snapshot in snapshots}
    # Held-position management is deliberately independent from new-entry
    # eligibility. A symbol can leave the ranked/trading universe and still be
    # marked and exited in PAPER as long as fresh public candles exist.
    for symbol in sorted(requested - set(candles_by_symbol)):
        try:
            raw = market.daily_candles(symbol, settings.history_limit)
            closed = [candle for candle in raw if candle.close_time < timestamp]
            candles = validate_daily_candles(closed, now_ms=timestamp)
            if len(candles) < settings.minimum_history:
                raise DataValidationError("insufficient history")
        except (MarketDataError, DataValidationError) as exc:
            raise DataValidationError(
                f"unable to validate open PAPER position data for {symbol}: {exc}"
            ) from None
        candles_by_symbol[symbol] = candles
    decision_list = [
        evaluate(asset.symbol, candles_by_symbol[asset.symbol], settings)
        if asset.entry_eligible or asset.symbol in requested
        else Decision(
            signal="RANKED_NOT_ELIGIBLE",
            symbol=asset.symbol,
            strategy_version=settings.strategy_version,
            candle_close_time=candles_by_symbol[asset.symbol][-1].close_time,
            reasons=asset.eligibility_reasons,
            metrics={"rank_score": asset.score},
            risk_plan=None,
        )
        for asset in ranking
    ]
    ranked_decisions = {decision.symbol for decision in decision_list}
    decision_list.extend(
        evaluate(symbol, candles_by_symbol[symbol], settings)
        for symbol in sorted(requested - ranked_decisions)
        if symbol in candles_by_symbol
    )
    decisions = tuple(decision_list)
    market_bars = tuple(
        MarketBar(
            symbol=symbol,
            open_time=candles[-1].open_time,
            close_time=candles[-1].close_time,
            open=candles[-1].open,
            high=candles[-1].high,
            low=candles[-1].low,
            close=candles[-1].close,
        )
        for symbol, candles in sorted(candles_by_symbol.items())
    )

    session_profiles: list[AssetSessionProfile] = []
    session_skipped: list[str] = []
    session_symbols = {asset.symbol for asset in ranking} | requested
    for symbol in sorted(session_symbols):
        try:
            if symbol not in spreads:
                raise DataValidationError("current spread unavailable")
            raw_hourly = market.hourly_candles(symbol, settings.hourly_history_limit)
            closed_hourly = [candle for candle in raw_hourly if candle.close_time < timestamp]
            hourly = validate_hourly_candles(closed_hourly, now_ms=timestamp)
            session_profiles.append(
                analyze_sessions(
                    symbol,
                    hourly,
                    spread_pct=spreads[symbol],
                    timezone=settings.report_timezone,
                    minimum_samples=settings.minimum_hour_samples,
                )
            )
        except (MarketDataError, DataValidationError, ValueError):
            session_skipped.append(symbol)
            continue
    return ObservationReport(
        generated_at_ms=timestamp,
        mode="OBSERVATION",
        strategy_version=settings.strategy_version,
        market_data_source=settings.market_data_source,
        future_execution_target=settings.future_execution_target,
        universe_count=len(universe),
        prefiltered_count=len(prefiltered),
        analyzed_count=len(candles_by_symbol),
        skipped_symbols=tuple(sorted(skipped)),
        skipped_reasons=skipped_reasons,
        ranking=tuple(ranking),
        decisions=decisions,
        market_bars=market_bars,
        session_profiles=tuple(session_profiles),
        session_skipped_symbols=tuple(session_skipped),
    )
