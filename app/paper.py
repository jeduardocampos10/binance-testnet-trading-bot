"""Persistent PAPER portfolio simulator. It has no authenticated Binance code."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings, settings_fingerprint
from .models import DAY_MS
from .observer import ObservationReport
from .state import AuditStore

HUNDRED = Decimal("100")


def _text(value: Decimal) -> str:
    return format(value, "f")


def _day_utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).date().isoformat()


def _equity(store: AuditStore, settings: Settings) -> Decimal:
    account = store.paper_account()
    exit_fee_rate = settings.fee_per_side_pct / HUNDRED
    return Decimal(account["cash"]) + sum(
        (
            Decimal(position["quantity"])
            * Decimal(position["last_mark"])
            * (Decimal("1") - exit_fee_rate)
            for position in store.open_positions()
        ),
        Decimal("0"),
    )


def _event(
    *, key: str, day: str, timestamp_ms: int, event_type: str, symbol: str | None, payload: dict[str, Any]
) -> dict[str, Any]:
    return {
        "event_key": key,
        "day_utc": day,
        "generated_at_ms": timestamp_ms,
        "event_type": event_type,
        "symbol": symbol,
        "payload": payload,
    }


def _sell_fill(reference: Decimal, settings: Settings) -> Decimal:
    return reference * (Decimal("1") - settings.slippage_per_side_pct / HUNDRED)


def _buy_fill(reference: Decimal, settings: Settings) -> Decimal:
    return reference * (Decimal("1") + settings.slippage_per_side_pct / HUNDRED)


def _loss_pct(equity: Decimal, start_equity: Decimal) -> Decimal:
    return (equity / start_equity - Decimal("1")) * HUNDRED if start_equity > 0 else Decimal("-100")


def _run_paper_cycle_locked(report: ObservationReport, settings: Settings, store: AuditStore) -> dict[str, Any]:
    """Apply a validated, exclusively claimed closed candle."""
    timestamp = report.generated_at_ms
    cycle_close_time = report.market_bars[0].close_time
    day = _day_utc(report.market_bars[0].open_time)
    config_hash = settings_fingerprint(settings)
    store.ensure_paper_account(_text(settings.paper_equity_usdt), config_hash)
    account = store.paper_account()
    control = store.paper_control()
    if Decimal(account["initial_equity"]) != settings.paper_equity_usdt:
        raise ValueError("configured PAPER equity differs from the initialized account")

    store.ensure_paper_day(day, _text(_equity(store, settings)))
    day_state = store.paper_day(day)
    start_equity = Decimal(day_state["start_equity"])
    bars = {bar.symbol: bar for bar in report.market_bars}
    decisions = {decision.symbol: decision for decision in report.decisions}

    # Existing positions are always managed before considering entries. A daily
    # lock never disables protective exits.
    for position in store.open_positions():
        symbol = position["symbol"]
        bar = bars.get(symbol)
        if bar is None:
            raise ValueError(f"missing closed market bar for open PAPER position {symbol}")
        entry = Decimal(position["entry_price"])
        quantity = Decimal(position["quantity"])
        hard_stop = Decimal(position["hard_stop"])
        previous_peak = Decimal(position["peak_price"])
        previous_trailing = Decimal(position["trailing_stop"]) if position["trailing_stop"] is not None else None
        active_stop = max(hard_stop, previous_trailing) if previous_trailing is not None else hard_stop

        exit_reference: Decimal | None = None
        exit_reason: str | None = None
        # If the candle opens through a stop, simulate the gap at the open.
        if bar.low <= active_stop:
            exit_reference = min(active_stop, bar.open)
            exit_reason = "PROTECTIVE_STOP"
        else:
            decision = decisions.get(symbol)
            if decision is not None and decision.signal == "EXIT_CANDIDATE":
                exit_reference = bar.close
                exit_reason = "STRATEGY_EXIT"

        if exit_reference is not None and exit_reason is not None:
            fill = _sell_fill(exit_reference, settings)
            proceeds = quantity * fill
            fee = proceeds * settings.fee_per_side_pct / HUNDRED
            cash = Decimal(store.paper_account()["cash"]) + proceeds - fee
            entry_cost = quantity * entry + Decimal(position["entry_fee"])
            pnl = proceeds - fee - entry_cost
            event = _event(
                key=f"{cycle_close_time}:{symbol}:EXIT:{position['id']}",
                day=day,
                timestamp_ms=timestamp,
                event_type="PAPER_EXIT",
                symbol=symbol,
                payload={
                    "reason": exit_reason,
                    "reference_price": _text(exit_reference),
                    "fill_price": _text(fill),
                    "quantity": _text(quantity),
                    "fee_usdt": _text(fee),
                    "realized_pnl_usdt": _text(pnl),
                },
            )
            store.close_paper_position(
                int(position["id"]),
                exit_time_ms=timestamp,
                exit_price=_text(fill),
                exit_fee=_text(fee),
                exit_reason=exit_reason,
                event=event,
                new_cash=_text(cash),
            )
            continue

        peak = max(previous_peak, bar.high)
        trailing = previous_trailing
        if peak > entry:
            candidate_trailing = peak * (Decimal("1") - settings.trailing_stop_pct / HUNDRED)
            trailing = max(trailing, candidate_trailing) if trailing is not None else candidate_trailing
        store.update_paper_position(
            int(position["id"]),
            peak_price=_text(peak),
            trailing_stop=_text(trailing) if trailing is not None else None,
            last_mark=_text(bar.close),
        )

    equity = _equity(store, settings)
    day_state = store.paper_day(day)
    if not day_state["locked"] and _loss_pct(equity, start_equity) <= -settings.daily_loss_limit_pct:
        store.lock_paper_day(day, "daily equity loss limit reached; entries blocked, protective exits remain active")

    entry_eligible_symbols = {asset.symbol for asset in report.ranking if asset.entry_eligible}
    entry_candidates = [
        decision
        for decision in report.decisions
        if decision.signal == "ENTRY_CANDIDATE" and decision.symbol in entry_eligible_symbols
    ]
    if any(decision.risk_plan is None for decision in entry_candidates):
        raise ValueError("ENTRY_CANDIDATE without a validated risk plan")
    for decision in entry_candidates:
        day_state = store.paper_day(day)
        if control["paused"]:
            store.record_paper_event(
                _event(
                    key=f"{cycle_close_time}:{decision.symbol}:PAUSED_SIMULATION",
                    day=day,
                    timestamp_ms=timestamp,
                    event_type="PAUSED_ENTRY_SIMULATION",
                    symbol=decision.symbol,
                    payload={
                        "reference_price": _text(decision.metrics["close"]),
                        "reason": control["reason"] or "new PAPER entries manually paused",
                        "would_have_entered": True,
                    },
                )
            )
            continue
        if day_state["locked"]:
            store.record_paper_event(
                _event(
                    key=f"{cycle_close_time}:{decision.symbol}:LOCKED_SIMULATION",
                    day=day,
                    timestamp_ms=timestamp,
                    event_type="LOCKED_ENTRY_SIMULATION",
                    symbol=decision.symbol,
                    payload={
                        "reference_price": _text(decision.metrics["close"]),
                        "reason": day_state["lock_reason"],
                        "would_have_entered": True,
                    },
                )
            )
            continue
        if store.paper_entry_exists(decision.symbol, day):
            continue
        positions = store.open_positions()
        if len(positions) >= settings.max_positions:
            store.record_paper_event(
                _event(
                    key=f"{cycle_close_time}:{decision.symbol}:CAPACITY_BLOCK",
                    day=day,
                    timestamp_ms=timestamp,
                    event_type="ENTRY_BLOCKED_CAPACITY",
                    symbol=decision.symbol,
                    payload={"max_positions": settings.max_positions},
                )
            )
            continue
        if any(position["symbol"] == decision.symbol for position in positions):
            continue

        reference = decision.metrics["close"]
        fill = _buy_fill(reference, settings)
        current_equity = _equity(store, settings)
        allocation = current_equity * settings.max_exposure_pct / HUNDRED
        cash = Decimal(store.paper_account()["cash"])
        fee_rate = settings.fee_per_side_pct / HUNDRED
        allocation = min(allocation, cash / (Decimal("1") + fee_rate))
        if allocation <= 0:
            continue
        quantity = allocation / fill
        fee = allocation * fee_rate
        new_cash = cash - allocation - fee
        hard_stop = fill * (Decimal("1") - settings.hard_stop_pct / HUNDRED)
        position = {
            "symbol": decision.symbol,
            "entry_day": day,
            "entry_time_ms": timestamp,
            "entry_candle_close_time": cycle_close_time,
            "entry_price": _text(fill),
            "quantity": _text(quantity),
            "entry_fee": _text(fee),
            "hard_stop": _text(hard_stop),
            "trailing_stop": None,
            "peak_price": _text(fill),
            "last_mark": _text(reference),
        }
        event = _event(
            key=f"{cycle_close_time}:{decision.symbol}:ENTRY",
            day=day,
            timestamp_ms=timestamp,
            event_type="PAPER_ENTRY",
            symbol=decision.symbol,
            payload={
                "reference_price": _text(reference),
                "fill_price": _text(fill),
                "quantity": _text(quantity),
                "gross_usdt": _text(allocation),
                "fee_usdt": _text(fee),
                "hard_stop": _text(hard_stop),
            },
        )
        store.open_paper_position(position, event, _text(new_cash))

        equity = _equity(store, settings)
        if _loss_pct(equity, start_equity) <= -settings.daily_loss_limit_pct:
            store.lock_paper_day(day, "daily equity loss limit reached; entries blocked, protective exits remain active")

    equity = _equity(store, settings)
    day_state = store.paper_day(day)
    positions_payload = []
    unrealized = Decimal("0")
    for position in store.open_positions():
        quantity = Decimal(position["quantity"])
        entry = Decimal(position["entry_price"])
        mark = Decimal(position["last_mark"])
        estimated_exit_fee = quantity * mark * settings.fee_per_side_pct / HUNDRED
        pnl = quantity * (mark - entry) - Decimal(position["entry_fee"]) - estimated_exit_fee
        unrealized += pnl
        positions_payload.append(
            {
                "symbol": position["symbol"],
                "quantity": position["quantity"],
                "entry_price": position["entry_price"],
                "mark_price": position["last_mark"],
                "hard_stop": position["hard_stop"],
                "trailing_stop": position["trailing_stop"],
                "unrealized_pnl_usdt": _text(pnl),
            }
        )
    events = store.paper_events_for_day(day)
    realized = sum(
        (Decimal(event["payload"]["realized_pnl_usdt"]) for event in events if event["event_type"] == "PAPER_EXIT"),
        Decimal("0"),
    )
    local_time = datetime.fromtimestamp(timestamp / 1000, tz=timezone.utc).astimezone(ZoneInfo(settings.report_timezone))
    profiles = {profile.symbol: profile.as_json_dict() for profile in report.session_profiles}
    payload: dict[str, Any] = {
        "mode": "PAPER",
        "generated_at_ms": timestamp,
        "report_time": local_time.isoformat(),
        "report_timezone": settings.report_timezone,
        "risk_day_utc": day,
        "strategy_version": settings.strategy_version,
        "config_hash": config_hash,
        "market_data_source": settings.market_data_source,
        "execution_mode": "PAPER",
        "future_execution_target": settings.future_execution_target,
        "equity_usdt": _text(equity),
        "cash_usdt": store.paper_account()["cash"],
        "daily_pnl_usdt": _text(equity - start_equity),
        "daily_pnl_pct": _text(_loss_pct(equity, start_equity)),
        "cumulative_pnl_usdt": _text(equity - settings.paper_equity_usdt),
        "realized_pnl_today_usdt": _text(realized),
        "unrealized_pnl_usdt": _text(unrealized),
        "daily_lock": bool(day_state["locked"]),
        "daily_lock_reason": day_state["lock_reason"],
        "entries_paused": bool(control["paused"]),
        "pause_reason": control["reason"],
        "daily_goal_pct_informational": _text(settings.daily_goal_pct),
        "open_positions": positions_payload,
        "top_20": [asset.symbol for asset in report.ranking],
        "entry_candidates": [item.symbol for item in entry_candidates],
        "exit_candidates": [item.symbol for item in report.decisions if item.signal == "EXIT_CANDIDATE"],
        "favorable_hours": profiles,
        "events_today": events,
        "skipped_symbols": list(report.skipped_symbols),
        "session_skipped_symbols": list(report.session_skipped_symbols),
        "external_risk_context": "not configured; never treated as a positive signal",
        "testnet_orders_enabled": False,
    }
    store.record_paper_report(timestamp, day, payload)
    return payload


def run_paper_cycle(report: ObservationReport, settings: Settings, store: AuditStore) -> dict[str, Any]:
    """Atomically apply one unique closed daily candle to the PAPER account.

    Market-candle time, rather than report wall-clock time, is the mutation
    identity. Replays return the original result; stale candles fail closed.
    """
    if report.mode != "OBSERVATION" or report.strategy_version != settings.strategy_version:
        raise ValueError("PAPER report mode or strategy version mismatch")
    if (
        report.market_data_source != settings.market_data_source
        or report.future_execution_target != settings.future_execution_target
    ):
        raise ValueError("PAPER report data source or future target mismatch")
    if not report.market_bars:
        raise ValueError("PAPER report has no market bars")
    bar_symbols = [bar.symbol for bar in report.market_bars]
    decision_symbols = [decision.symbol for decision in report.decisions]
    if len(bar_symbols) != len(set(bar_symbols)) or len(decision_symbols) != len(set(decision_symbols)):
        raise ValueError("PAPER report contains duplicate symbols")
    close_times = {bar.close_time for bar in report.market_bars}
    open_times = {bar.open_time for bar in report.market_bars}
    if len(close_times) != 1 or len(open_times) != 1:
        raise ValueError("PAPER bars do not share one closed daily interval")
    cycle_close_time = next(iter(close_times))
    bars = {bar.symbol: bar for bar in report.market_bars}
    for decision in report.decisions:
        bar = bars.get(decision.symbol)
        if decision.strategy_version != settings.strategy_version:
            raise ValueError("PAPER decision strategy version mismatch")
        if bar is None or decision.candle_close_time != cycle_close_time:
            raise ValueError("PAPER decision does not match its market bar")
        if "close" in decision.metrics and decision.metrics["close"] != bar.close:
            raise ValueError("PAPER decision price does not match its market bar")

    bar_open_time = next(iter(open_times))
    expected_open_time = (report.generated_at_ms // DAY_MS) * DAY_MS - DAY_MS
    if (
        bar_open_time % DAY_MS != 0
        or cycle_close_time != bar_open_time + DAY_MS - 1
        or cycle_close_time >= report.generated_at_ms
        or bar_open_time != expected_open_time
    ):
        raise ValueError("PAPER report candle is not the latest closed UTC daily interval")

    config_hash = settings_fingerprint(settings)
    cached = store.begin_paper_cycle(
        cycle_close_time, settings.strategy_version, config_hash, report.generated_at_ms
    )
    if cached is not None:
        return cached
    try:
        payload = _run_paper_cycle_locked(report, settings, store)
        store.complete_paper_cycle(cycle_close_time, payload)
        return payload
    except Exception:
        store.abort_paper_cycle()
        raise
