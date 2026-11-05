from __future__ import annotations

from decimal import Decimal

from app.config import Settings
from app.universe_policy import MANDATORY_EXCLUDED_BASE_ASSETS
from app.models import Candle, DAY_MS, HOUR_MS


def settings() -> Settings:
    return Settings(
        quote_asset="USDT",
        strategy_version="test-v1",
        market_data_source="BINANCE_PUBLIC_PRODUCTION",
        future_execution_target="BINANCE_SPOT_TESTNET",
        excluded_base_assets=tuple(sorted(MANDATORY_EXCLUDED_BASE_ASSETS)),
        top_n=20,
        prefilter_n=60,
        history_limit=120,
        minimum_history=90,
        paper_equity_usdt=Decimal("100"),
        max_positions=3,
        max_exposure_pct=Decimal("10"),
        hard_stop_pct=Decimal("5"),
        trailing_stop_pct=Decimal("3"),
        daily_loss_limit_pct=Decimal("3"),
        reward_risk_ratio=Decimal("2"),
        daily_goal_pct=Decimal("1"),
        fee_per_side_pct=Decimal("0.1"),
        slippage_per_side_pct=Decimal("0.05"),
        rsi_min=Decimal("50"),
        rsi_max=Decimal("68"),
        minimum_volume_ratio=Decimal("0.8"),
        maximum_spread_pct=Decimal("0.5"),
        hourly_history_limit=480,
        minimum_hour_samples=5,
        report_timezone="America/Sao_Paulo",
    )


def candles_from_closes(closes: list[Decimal], *, start: int = 0) -> list[Candle]:
    candles: list[Candle] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        open_price = previous
        high = max(open_price, close) * Decimal("1.01")
        low = min(open_price, close) * Decimal("0.99")
        open_time = start + index * DAY_MS
        candles.append(
            Candle(
                open_time=open_time,
                close_time=open_time + DAY_MS - 1,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=Decimal("100"),
            )
        )
        previous = close
    return candles


def hourly_candles(count: int = 480, *, start: int = 0) -> list[Candle]:
    candles: list[Candle] = []
    price = Decimal("100")
    for index in range(count):
        open_time = start + index * HOUR_MS
        change = Decimal((index % 7) - 3) / Decimal("1000")
        close = price * (Decimal("1") + change)
        candles.append(
            Candle(
                open_time=open_time,
                close_time=open_time + HOUR_MS - 1,
                open=price,
                high=max(price, close) * Decimal("1.001"),
                low=min(price, close) * Decimal("0.999"),
                close=close,
                volume=Decimal(100 + (index % 24)),
            )
        )
        price = close
    return candles
