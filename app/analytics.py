"""Deterministic analytics over the local PAPER audit database."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .state import AuditStore

PERIOD_DAYS = {"day": 1, "week": 7, "month": 30, "all": None}


def _decimal(value: object, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("stored financial value is invalid") from None
    if not parsed.is_finite():
        raise ValueError("stored financial value is not finite")
    return parsed


def _text(value: Decimal) -> str:
    return format(value, "f")


def _position_pnl(position: dict[str, Any]) -> Decimal:
    quantity = _decimal(position["quantity"])
    entry_cost = quantity * _decimal(position["entry_price"]) + _decimal(position["entry_fee"])
    exit_value = quantity * _decimal(position["exit_price"]) - _decimal(position["exit_fee"])
    return exit_value - entry_cost


def _max_drawdown(equities: list[Decimal]) -> tuple[Decimal, Decimal]:
    if not equities:
        return Decimal("0"), Decimal("0")
    peak = equities[0]
    maximum_usdt = Decimal("0")
    maximum_pct = Decimal("0")
    for equity in equities:
        peak = max(peak, equity)
        drawdown = peak - equity
        pct = drawdown / peak * Decimal("100") if peak > 0 else Decimal("0")
        maximum_usdt = max(maximum_usdt, drawdown)
        maximum_pct = max(maximum_pct, pct)
    return maximum_usdt, maximum_pct


def build_analytics(store: AuditStore, period: str) -> dict[str, Any]:
    if period not in PERIOD_DAYS:
        raise ValueError("period must be day, week, month, or all")
    all_reports = store.paper_reports_since()
    latest_observation = store.latest_report() or {}
    latest_paper = all_reports[-1] if all_reports else None
    anchor_ms = int(latest_paper["generated_at_ms"]) if latest_paper else int(
        datetime.now(tz=timezone.utc).timestamp() * 1000
    )
    days = PERIOD_DAYS[period]
    if days is None:
        since_ms = None
    else:
        anchor_day = datetime.fromtimestamp(anchor_ms / 1000, tz=timezone.utc).date()
        start_day = anchor_day - timedelta(days=days - 1)
        since_ms = int(datetime.combine(start_day, datetime.min.time(), tzinfo=timezone.utc).timestamp() * 1000)
    reports = [
        report for report in all_reports
        if since_ms is None or int(report["generated_at_ms"]) >= since_ms
    ]
    trades = store.closed_paper_positions(since_ms)

    if reports:
        first_equity = _decimal(reports[0].get("equity_usdt")) - _decimal(reports[0].get("daily_pnl_usdt"))
        final_equity = _decimal(reports[-1].get("equity_usdt"))
    else:
        first_equity = final_equity = Decimal("0")
    pnl = final_equity - first_equity
    return_pct = pnl / first_equity * Decimal("100") if first_equity > 0 else Decimal("0")
    equity_curve = [first_equity] + [_decimal(report.get("equity_usdt")) for report in reports]
    drawdown_usdt, drawdown_pct = _max_drawdown(equity_curve)

    per_asset: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"trades": 0, "wins": 0, "losses": 0, "realized_pnl_usdt": Decimal("0")}
    )
    realized_total = Decimal("0")
    wins = 0
    for trade in trades:
        trade_pnl = _position_pnl(trade)
        realized_total += trade_pnl
        item = per_asset[trade["symbol"]]
        item["trades"] += 1
        item["realized_pnl_usdt"] += trade_pnl
        if trade_pnl > 0:
            item["wins"] += 1
            wins += 1
        elif trade_pnl < 0:
            item["losses"] += 1

    assets = [
        {
            "symbol": symbol,
            "trades": item["trades"],
            "wins": item["wins"],
            "losses": item["losses"],
            "realized_pnl_usdt": _text(item["realized_pnl_usdt"]),
        }
        for symbol, item in per_asset.items()
    ]
    assets.sort(key=lambda item: (_decimal(item["realized_pnl_usdt"]), item["symbol"]), reverse=True)
    best_asset = assets[0] if assets else None
    worst_asset = min(assets, key=lambda item: (_decimal(item["realized_pnl_usdt"]), item["symbol"])) if assets else None

    daily_series = [
        {
            "risk_day_utc": report.get("risk_day_utc"),
            "equity_usdt": report.get("equity_usdt"),
            "daily_pnl_usdt": report.get("daily_pnl_usdt"),
            "daily_pnl_pct": report.get("daily_pnl_pct"),
        }
        for report in reports
    ]
    ranking = latest_observation.get("ranking", [])
    decisions = latest_observation.get("decisions", [])
    emerging = [
        {
            "symbol": decision.get("symbol"),
            "signal": decision.get("signal"),
            "reasons": decision.get("reasons", []),
            "metrics": decision.get("metrics", {}),
        }
        for decision in decisions
        if decision.get("signal") in {"ENTRY_CANDIDATE", "EXIT_CANDIDATE", "ENTRY_BLOCKED"}
    ]
    eligible_ranked = [item for item in ranking if item.get("entry_eligible")]

    return {
        "period": period,
        "from_ms": since_ms,
        "to_ms": anchor_ms,
        "reports": len(reports),
        "start_equity_usdt": _text(first_equity),
        "final_equity_usdt": _text(final_equity),
        "period_pnl_usdt": _text(pnl),
        "period_return_pct": _text(return_pct),
        "realized_pnl_usdt": _text(realized_total),
        "unrealized_pnl_usdt": latest_paper.get("unrealized_pnl_usdt", "0") if latest_paper else "0",
        "max_drawdown_usdt": _text(drawdown_usdt),
        "max_drawdown_pct": _text(drawdown_pct),
        "closed_trades": len(trades),
        "wins": wins,
        "losses": len([trade for trade in trades if _position_pnl(trade) < 0]),
        "win_rate_pct": _text(Decimal(wins) / Decimal(len(trades)) * Decimal("100")) if trades else "0",
        "best_asset": best_asset,
        "worst_asset": worst_asset,
        "asset_performance": assets,
        "equity_series": daily_series,
        "open_positions": latest_paper.get("open_positions", []) if latest_paper else [],
        "daily_lock": latest_paper.get("daily_lock", False) if latest_paper else False,
        "entries_paused": store.paper_control()["paused"],
        "top_20": ranking,
        "eligible_ranked": eligible_ranked,
        "emerging_trends": emerging,
        "favorable_hours": latest_paper.get("favorable_hours", {}) if latest_paper else {},
        "skipped_reasons": latest_observation.get("skipped_reasons", {}),
        "market_data_source": latest_paper.get("market_data_source") if latest_paper else None,
        "execution_mode": "PAPER",
        "future_execution_target": latest_paper.get("future_execution_target") if latest_paper else None,
        "testnet_orders_enabled": False,
    }
