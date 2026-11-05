"""Non-secret observer configuration."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .universe_policy import (
    EFFECTIVE_MANDATORY_EXCLUDED_BASE_ASSETS,
    MANDATORY_EXCLUDED_BASE_ASSETS,
)


class ConfigurationError(ValueError):
    """Safe configuration failure."""


@dataclass(frozen=True)
class Settings:
    quote_asset: str
    strategy_version: str
    market_data_source: str
    future_execution_target: str
    excluded_base_assets: tuple[str, ...]
    top_n: int
    prefilter_n: int
    history_limit: int
    minimum_history: int
    paper_equity_usdt: Decimal
    max_positions: int
    max_exposure_pct: Decimal
    hard_stop_pct: Decimal
    trailing_stop_pct: Decimal
    daily_loss_limit_pct: Decimal
    reward_risk_ratio: Decimal
    daily_goal_pct: Decimal
    fee_per_side_pct: Decimal
    slippage_per_side_pct: Decimal
    rsi_min: Decimal
    rsi_max: Decimal
    minimum_volume_ratio: Decimal
    maximum_spread_pct: Decimal
    hourly_history_limit: int
    minimum_hour_samples: int
    report_timezone: str


def _decimal(payload: dict[str, object], key: str) -> Decimal:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ConfigurationError(f"{key} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise ConfigurationError(f"{key} is invalid") from None
    if not parsed.is_finite():
        raise ConfigurationError(f"{key} is invalid")
    return parsed


def load_settings(path: str | Path) -> Settings:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ConfigurationError("unable to read configuration") from None
    if not isinstance(payload, dict):
        raise ConfigurationError("configuration must be an object")

    quote_asset = payload.get("quote_asset")
    version = payload.get("strategy_version")
    top_n = payload.get("top_n")
    prefilter_n = payload.get("prefilter_n")
    history_limit = payload.get("history_limit")
    minimum_history = payload.get("minimum_history")
    max_positions = payload.get("max_positions")
    hourly_history_limit = payload.get("hourly_history_limit")
    minimum_hour_samples = payload.get("minimum_hour_samples")
    report_timezone = payload.get("report_timezone")
    market_data_source = payload.get("market_data_source")
    future_execution_target = payload.get("future_execution_target")
    excluded_base_assets = payload.get("excluded_base_assets")
    if quote_asset != "USDT":
        raise ConfigurationError("only the USDT quote asset is accepted")
    if not isinstance(version, str) or not version or len(version) > 64:
        raise ConfigurationError("invalid strategy_version")
    if market_data_source != "BINANCE_PUBLIC_PRODUCTION":
        raise ConfigurationError("market_data_source must be BINANCE_PUBLIC_PRODUCTION")
    if future_execution_target != "BINANCE_SPOT_TESTNET":
        raise ConfigurationError("future_execution_target must be BINANCE_SPOT_TESTNET")
    if (
        not isinstance(excluded_base_assets, list)
        or not excluded_base_assets
        or len(excluded_base_assets) > 100
        or any(not isinstance(item, str) or not item.isupper() or not item.isalnum() for item in excluded_base_assets)
        or len(excluded_base_assets) != len(set(excluded_base_assets))
    ):
        raise ConfigurationError("excluded_base_assets must be a unique uppercase asset list")
    missing_exclusions = MANDATORY_EXCLUDED_BASE_ASSETS - set(excluded_base_assets)
    if missing_exclusions:
        raise ConfigurationError("mandatory crypto-only exclusions are missing")
    if not isinstance(top_n, int) or not 1 <= top_n <= 20:
        raise ConfigurationError("top_n must be between 1 and 20")
    if not isinstance(prefilter_n, int) or not top_n <= prefilter_n <= 100:
        raise ConfigurationError("prefilter_n must be between top_n and 100")
    if not isinstance(history_limit, int) or not 90 <= history_limit <= 1000:
        raise ConfigurationError("history_limit must be between 90 and 1000")
    if not isinstance(minimum_history, int) or not 90 <= minimum_history <= history_limit:
        raise ConfigurationError("minimum_history must be between 90 and history_limit")
    if not isinstance(max_positions, int) or not 1 <= max_positions <= 3:
        raise ConfigurationError("max_positions must be between 1 and 3")
    if not isinstance(hourly_history_limit, int) or not 480 <= hourly_history_limit <= 1000:
        raise ConfigurationError("hourly_history_limit must be between 480 and 1000")
    if not isinstance(minimum_hour_samples, int) or not 5 <= minimum_hour_samples <= 40:
        raise ConfigurationError("minimum_hour_samples must be between 5 and 40")
    if report_timezone != "America/Sao_Paulo":
        raise ConfigurationError("report_timezone must be America/Sao_Paulo")

    settings = Settings(
        quote_asset=quote_asset,
        strategy_version=version,
        market_data_source=market_data_source,
        future_execution_target=future_execution_target,
        excluded_base_assets=tuple(sorted(excluded_base_assets)),
        top_n=top_n,
        prefilter_n=prefilter_n,
        history_limit=history_limit,
        minimum_history=minimum_history,
        paper_equity_usdt=_decimal(payload, "paper_equity_usdt"),
        max_positions=max_positions,
        max_exposure_pct=_decimal(payload, "max_exposure_pct"),
        hard_stop_pct=_decimal(payload, "hard_stop_pct"),
        trailing_stop_pct=_decimal(payload, "trailing_stop_pct"),
        daily_loss_limit_pct=_decimal(payload, "daily_loss_limit_pct"),
        reward_risk_ratio=_decimal(payload, "reward_risk_ratio"),
        daily_goal_pct=_decimal(payload, "daily_goal_pct"),
        fee_per_side_pct=_decimal(payload, "fee_per_side_pct"),
        slippage_per_side_pct=_decimal(payload, "slippage_per_side_pct"),
        rsi_min=_decimal(payload, "rsi_min"),
        rsi_max=_decimal(payload, "rsi_max"),
        minimum_volume_ratio=_decimal(payload, "minimum_volume_ratio"),
        maximum_spread_pct=_decimal(payload, "maximum_spread_pct"),
        hourly_history_limit=hourly_history_limit,
        minimum_hour_samples=minimum_hour_samples,
        report_timezone=report_timezone,
    )
    if settings.paper_equity_usdt <= 0:
        raise ConfigurationError("paper_equity_usdt must be positive")
    if not Decimal("0") < settings.max_exposure_pct <= Decimal("25"):
        raise ConfigurationError("max_exposure_pct must be in (0, 25]")
    if not Decimal("0") < settings.hard_stop_pct <= Decimal("10"):
        raise ConfigurationError("hard_stop_pct must be in (0, 10]")
    if not Decimal("0") < settings.trailing_stop_pct < settings.hard_stop_pct:
        raise ConfigurationError("trailing_stop_pct must be below hard_stop_pct")
    if not Decimal("0") < settings.daily_loss_limit_pct <= Decimal("5"):
        raise ConfigurationError("daily_loss_limit_pct must be in (0, 5]")
    if settings.reward_risk_ratio < Decimal("1.5"):
        raise ConfigurationError("reward_risk_ratio must be at least 1.5")
    if settings.daily_goal_pct < 0:
        raise ConfigurationError("daily_goal_pct cannot be negative")
    if any(value < 0 for value in (settings.fee_per_side_pct, settings.slippage_per_side_pct)):
        raise ConfigurationError("cost assumptions cannot be negative")
    if not Decimal("0") <= settings.rsi_min < settings.rsi_max <= Decimal("100"):
        raise ConfigurationError("invalid RSI bounds")
    if not Decimal("0") <= settings.minimum_volume_ratio <= Decimal("5"):
        raise ConfigurationError("invalid minimum_volume_ratio")
    if not Decimal("0") < settings.maximum_spread_pct <= Decimal("2"):
        raise ConfigurationError("maximum_spread_pct must be in (0, 2]")
    return settings


def settings_fingerprint(settings: Settings) -> str:
    """Return a stable hash for all strategy, risk, cost, and report settings."""
    payload = {
        key: format(value, "f") if isinstance(value, Decimal) else value
        for key, value in asdict(settings).items()
    }
    payload["effective_mandatory_excluded_base_assets"] = sorted(
        EFFECTIVE_MANDATORY_EXCLUDED_BASE_ASSETS
    )
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
