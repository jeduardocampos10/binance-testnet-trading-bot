"""Persistent PAPER portfolio simulator. It has no authenticated Binance code."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings, settings_fingerprint
from .models import DAY_MS
from .nexus import build_nexus_review
from .observer import ObservationReport
from .probability import (
    HORIZON_CANDLES,
    GateEvidence,
    ProbabilityModelError,
    evaluate_gate,
    extract_features,
    net_outcome,
)
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
    store.ensure_paper_account(_text(settings.paper_equity_usdt), config_hash, settings.nexus_mode)
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
        # A previously active stop is treated as resident for the whole candle.
        # If its low is crossed, do not use a later/earlier unknown high to claim
        # a new profit floor: daily OHLC cannot prove the position survived.
        # An opening gap fills at the open; otherwise it fills at the old stop.
        if bar.low <= active_stop:
            exit_reference = min(active_stop, bar.open)
            exit_reason = "PROTECTIVE_STOP"

        if exit_reference is not None and exit_reason is not None:
            _close_position(
                store, settings, position, symbol=symbol,
                exit_reference=exit_reference, exit_reason=exit_reason,
                cycle_close_time=cycle_close_time, day=day, timestamp=timestamp,
            )
            continue

        peak = max(previous_peak, bar.high)
        trailing = previous_trailing
        if peak > entry:
            candidate_trailing = peak * (Decimal("1") - settings.trailing_stop_pct / HUNDRED)
            trailing = max(trailing, candidate_trailing) if trailing is not None else candidate_trailing
            peak_gain_pct = (peak / entry - Decimal("1")) * HUNDRED
            if peak_gain_pct >= settings.profit_lock_activation_pct:
                entry_cost = quantity * entry + Decimal(position["entry_fee"])
                target_net = entry_cost * (
                    Decimal("1") + settings.minimum_locked_profit_pct / HUNDRED
                )
                sell_cost_factor = (
                    (Decimal("1") - settings.slippage_per_side_pct / HUNDRED)
                    * (Decimal("1") - settings.fee_per_side_pct / HUNDRED)
                )
                profit_floor_reference = target_net / (quantity * sell_cost_factor)
                trailing = max(trailing, profit_floor_reference)

        # A high followed by a close below the newly raised stop proves that the
        # stop was crossed after activation, even though daily OHLC cannot order
        # an intraday low that later recovered above the stop.
        if trailing is not None and trailing > active_stop and bar.close <= trailing:
            _close_position(
                store, settings, position, symbol=symbol,
                exit_reference=trailing, exit_reason="PROTECTIVE_STOP_SAME_CANDLE",
                cycle_close_time=cycle_close_time, day=day, timestamp=timestamp,
            )
            continue
        decision = decisions.get(symbol)
        if decision is not None and decision.signal == "EXIT_CANDIDATE":
            _close_position(
                store, settings, position, symbol=symbol,
                exit_reference=bar.close, exit_reason="STRATEGY_EXIT",
                cycle_close_time=cycle_close_time, day=day, timestamp=timestamp,
            )
            continue
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

    gate_evidence: dict[str, GateEvidence] = {}
    if settings.probability_gate_mode != "OFF":
        # Capture every available bar first, then mature only labels whose fifth
        # subsequent bar is now present. Candidate features remain prospective.
        for bar in (*report.market_bars, *report.probability_bars):
            store.record_probability_bar(bar.symbol, bar.close_time, _text(bar.close))
        for candidate in store.unlabeled_probability_candidates():
            outcome_time = int(candidate["candle_close_time"]) + HORIZON_CANDLES * DAY_MS
            outcome_close = store.probability_bar_close(candidate["symbol"], outcome_time)
            if outcome_close is not None:
                label, net_return = net_outcome(
                    Decimal(candidate["entry_close"]), Decimal(outcome_close), settings
                )
                store.label_probability_candidate(
                    int(candidate["id"]), label=label,
                    outcome_close_time=outcome_time, net_return=net_return,
                )
        for decision in entry_candidates:
            try:
                features = extract_features(decision.metrics)
                store.record_probability_candidate(
                    symbol=decision.symbol,
                    candle_close_time=decision.candle_close_time,
                    strategy_version=settings.strategy_version,
                    config_hash=config_hash,
                    entry_close=_text(decision.metrics["close"]),
                    features=features,
                )
                gate_evidence[decision.symbol] = evaluate_gate(features, settings)
            except (KeyError, ValueError, ProbabilityModelError) as exc:
                gate_evidence[decision.symbol] = GateEvidence(
                    "UNAVAILABLE", None, 0, 0, None,
                    float(settings.probability_threshold),
                    float(settings.probability_wilson_threshold),
                    settings.probability_min_support,
                    False, str(exc), None,
                )
    else:
        for decision in entry_candidates:
            gate_evidence[decision.symbol] = GateEvidence(
                "OFF", None, 0, 0, None,
                float(settings.probability_threshold),
                float(settings.probability_wilson_threshold),
                settings.probability_min_support,
                True, "probability gate disabled", None,
            )

    selected_probability_candidates: list[str] = []
    if settings.probability_gate_mode == "REQUIRED":
        decision_order = {decision.symbol: index for index, decision in enumerate(entry_candidates)}
        qualified = [decision for decision in entry_candidates if gate_evidence[decision.symbol].allowed]
        qualified.sort(
            key=lambda decision: (
                -(gate_evidence[decision.symbol].probability or 0.0),
                -(gate_evidence[decision.symbol].wilson_lower_bound or 0.0),
                -gate_evidence[decision.symbol].support,
                decision_order[decision.symbol],
            )
        )
        selected_probability_candidates = [
            decision.symbol for decision in qualified[:settings.probability_max_candidates]
        ]
        selected = set(selected_probability_candidates)
        for decision in qualified[settings.probability_max_candidates:]:
            gate_evidence[decision.symbol] = replace(
                gate_evidence[decision.symbol],
                allowed=False,
                reason=f"qualified candidate limit of {settings.probability_max_candidates} reached",
            )

    for decision in entry_candidates:
        evidence = gate_evidence[decision.symbol]
        if settings.probability_gate_mode == "REQUIRED" and not evidence.allowed:
            store.record_paper_event(
                _event(
                    key=f"{cycle_close_time}:{decision.symbol}:PROBABILITY_BLOCK",
                    day=day,
                    timestamp_ms=timestamp,
                    event_type="ENTRY_BLOCKED_PROBABILITY",
                    symbol=decision.symbol,
                    payload={"probability_gate": evidence.as_json_dict(), "reason": evidence.reason},
                )
            )
            continue
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
                        "probability_gate": evidence.as_json_dict(),
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
                        "probability_gate": evidence.as_json_dict(),
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
                    payload={
                        "max_positions": settings.max_positions,
                        "probability_gate": evidence.as_json_dict(),
                    },
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
        nexus_permit_id: int | None = None
        if settings.nexus_mode != "OFF":
            review = build_nexus_review(
                decision,
                evidence,
                settings,
                config_hash,
                paused=bool(control["paused"]),
                day_locked=bool(day_state["locked"]),
                open_symbols=tuple(item["symbol"] for item in positions),
                open_count=len(positions),
                cash=cash,
                allocation=allocation + fee,
                selected_by_probability=(
                    settings.probability_gate_mode != "REQUIRED"
                    or decision.symbol in selected_probability_candidates
                ),
            )
            review_id = store.record_nexus_review(review)
            if settings.nexus_mode == "ENFORCE" and review["disposition"] != "PERMIT_ELIGIBLE":
                store.record_paper_event(
                    _event(
                        key=f"{cycle_close_time}:{decision.symbol}:NEXUS_BLOCK",
                        day=day,
                        timestamp_ms=timestamp,
                        event_type="ENTRY_BLOCKED_NEXUS",
                        symbol=decision.symbol,
                        payload={
                            "review_hash": review["review_hash"],
                            "disposition": review["disposition"],
                            "reason_codes": review["reason_codes"],
                        },
                    )
                )
                continue
            if settings.nexus_mode == "ENFORCE":
                nexus_permit_id = store.issue_paper_permit(review_id)
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
                "nexus_permit_id": nexus_permit_id,
                "probability_gate": evidence.as_json_dict(),
            },
        )
        store.open_paper_position(position, event, _text(new_cash), nexus_permit_id)

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
        "maxx_nexus": (
            {"mode": "OFF", "status": "OFF", "orders_enabled": False}
            if settings.nexus_mode == "OFF"
            else {"mode": settings.nexus_mode, **store.nexus_status(limit=20)}
        ),
        "profit_lock": {
            "activation_pct": _text(settings.profit_lock_activation_pct),
            "minimum_locked_profit_pct": _text(settings.minimum_locked_profit_pct),
            "gap_guarantee": False,
        },
        "probability_gate": {
            "mode": settings.probability_gate_mode,
            "probability_threshold": _text(settings.probability_threshold),
            "wilson_threshold": _text(settings.probability_wilson_threshold),
            "minimum_support": settings.probability_min_support,
            "maximum_candidates": settings.probability_max_candidates,
            "selected_candidates": selected_probability_candidates,
            "candidates": {symbol: evidence.as_json_dict() for symbol, evidence in sorted(gate_evidence.items())},
            "status": store.probability_status() if settings.probability_gate_mode != "OFF" else {"status": "OFF"},
            "guarantee": False,
        },
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
    # Operational mode is sticky even when this candle is a cached replay.
    store.assert_nexus_mode_not_downgraded(settings.nexus_mode)
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


def _close_position(
    store: AuditStore,
    settings: Settings,
    position: dict[str, Any],
    *,
    symbol: str,
    exit_reference: Decimal,
    exit_reason: str,
    cycle_close_time: int,
    day: str,
    timestamp: int,
) -> None:
    quantity = Decimal(position["quantity"])
    entry = Decimal(position["entry_price"])
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
    store.record_post_trade_audit(
        int(position["id"]),
        {
            "schema_version": 1,
            "status": (
                "LEGACY_NOT_APPLICABLE"
                if position["nexus_permit_id"] is None
                else (
                    "VERIFIED"
                    if store.verify_consumed_nexus_permit(
                        int(position["id"]), int(position["nexus_permit_id"])
                    )
                    else "INTEGRITY_ERROR"
                )
            ),
            "symbol": symbol,
            "nexus_permit_id": position["nexus_permit_id"],
            "exit_reason": exit_reason,
            "realized_pnl_usdt": _text(pnl),
            "orders_enabled": False,
        },
    )
