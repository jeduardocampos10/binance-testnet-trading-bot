"""Observation-only trend/crossover strategy and hypothetical risk plan."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal

from .config import Settings
from .indicators import atr, ema_series, rsi, sma
from .models import Candle


@dataclass(frozen=True)
class RiskPlan:
    entry: Decimal
    stop: Decimal
    target: Decimal
    stop_distance_pct: Decimal
    risk_budget_usdt: Decimal
    maximum_position_usdt: Decimal
    planned_position_usdt: Decimal
    quantity: Decimal
    reward_risk_ratio: Decimal


@dataclass(frozen=True)
class Decision:
    signal: str
    symbol: str
    strategy_version: str
    candle_close_time: int
    reasons: tuple[str, ...]
    metrics: dict[str, Decimal]
    risk_plan: RiskPlan | None

    def as_json_dict(self) -> dict[str, object]:
        def text(value: Decimal) -> str:
            return format(value, "f")

        payload: dict[str, object] = {
            "mode": "OBSERVATION",
            "signal": self.signal,
            "symbol": self.symbol,
            "strategy_version": self.strategy_version,
            "candle_close_time": self.candle_close_time,
            "reasons": list(self.reasons),
            "metrics": {key: text(value) for key, value in self.metrics.items()},
            "risk_plan": None,
        }
        if self.risk_plan is not None:
            payload["risk_plan"] = {
                key: text(value) if isinstance(value, Decimal) else value
                for key, value in asdict(self.risk_plan).items()
            }
        return payload


def _risk_plan(candles: list[Candle], settings: Settings, entry: Decimal, atr14: Decimal) -> tuple[RiskPlan | None, str | None]:
    # A fixed portfolio allocation and hard price stop keep the two kinds of
    # percentage explicit. High-volatility coins are rejected rather than
    # receiving a wider stop or a larger loss budget.
    atr_guard_pct = Decimal("2") * atr14 / entry * Decimal("100")
    if atr_guard_pct > settings.hard_stop_pct:
        return None, "risk plan rejected: two ATR exceed the hard-stop distance"
    stop = entry * (Decimal("1") - settings.hard_stop_pct / Decimal("100"))
    distance = entry - stop
    distance_pct = settings.hard_stop_pct

    maximum_position = settings.paper_equity_usdt * settings.max_exposure_pct / Decimal("100")
    quantity = maximum_position / entry
    if quantity <= 0:
        return None, "risk plan rejected: non-positive quantity"
    planned_position = quantity * entry
    risk_budget = planned_position * settings.hard_stop_pct / Decimal("100")

    target_by_r = entry + distance * settings.reward_risk_ratio
    round_trip_cost_pct = Decimal("2") * (settings.fee_per_side_pct + settings.slippage_per_side_pct)
    minimum_gross_return_pct = settings.daily_goal_pct + round_trip_cost_pct
    target_by_goal = entry * (Decimal("1") + minimum_gross_return_pct / Decimal("100"))
    target = max(target_by_r, target_by_goal)
    actual_rr = (target - entry) / distance
    return (
        RiskPlan(
            entry=entry,
            stop=stop,
            target=target,
            stop_distance_pct=distance_pct,
            risk_budget_usdt=risk_budget,
            maximum_position_usdt=maximum_position,
            planned_position_usdt=planned_position,
            quantity=quantity,
            reward_risk_ratio=actual_rr,
        ),
        None,
    )


def evaluate(symbol: str, candles: list[Candle], settings: Settings) -> Decision:
    if len(candles) < settings.minimum_history:
        raise ValueError("insufficient closed-candle history")

    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    current_sma30 = sma(closes, 30)
    current_sma60 = sma(closes, 60)
    sma60_five_ago = sma(closes[:-5], 60)
    ema7 = ema_series(closes, 7)
    ema15 = ema_series(closes, 15)
    current_ema7, previous_ema7 = ema7[-1], ema7[-2]
    current_ema15, previous_ema15 = ema15[-1], ema15[-2]
    current_rsi = rsi(closes, 14)
    current_atr = atr(candles, 14)
    average_volume20 = sma(volumes, 20)
    volume_ratio = candles[-1].volume / average_volume20 if average_volume20 > 0 else Decimal("0")
    close = closes[-1]

    metrics = {
        "open": candles[-1].open,
        "high": candles[-1].high,
        "low": candles[-1].low,
        "close": close,
        "sma30": current_sma30,
        "sma60": current_sma60,
        "sma60_five_ago": sma60_five_ago,
        "ema7": current_ema7,
        "ema15": current_ema15,
        "rsi14": current_rsi,
        "atr14": current_atr,
        "volume_ratio20": volume_ratio,
        "daily_goal_pct_informational": settings.daily_goal_pct,
    }

    crossed_up = previous_ema7 <= previous_ema15 and current_ema7 > current_ema15
    crossed_down = previous_ema7 >= previous_ema15 and current_ema7 < current_ema15
    entry_conditions = {
        "close above SMA60": close > current_sma60,
        "SMA30 above SMA60": current_sma30 > current_sma60,
        "SMA60 rising over five candles": current_sma60 > sma60_five_ago,
        "EMA7 crossed above EMA15": crossed_up,
        "RSI14 inside configured band": settings.rsi_min <= current_rsi <= settings.rsi_max,
        "volume confirms signal": volume_ratio >= settings.minimum_volume_ratio,
    }

    if crossed_down or close < current_sma30:
        reasons = []
        if crossed_down:
            reasons.append("EMA7 crossed below EMA15")
        if close < current_sma30:
            reasons.append("close below SMA30")
        return Decision("EXIT_CANDIDATE", symbol, settings.strategy_version, candles[-1].close_time, tuple(reasons), metrics, None)

    failed = tuple(name for name, passed in entry_conditions.items() if not passed)
    if failed:
        return Decision("NO_ENTRY", symbol, settings.strategy_version, candles[-1].close_time, failed, metrics, None)

    plan, rejection = _risk_plan(candles, settings, close, current_atr)
    if plan is None:
        return Decision("ENTRY_BLOCKED", symbol, settings.strategy_version, candles[-1].close_time, (rejection or "risk plan rejected",), metrics, None)
    return Decision("ENTRY_CANDIDATE", symbol, settings.strategy_version, candles[-1].close_time, ("all entry and risk conditions passed",), metrics, plan)
