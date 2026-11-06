"""Fresh, auditable Top-10 manual analysis. This module cannot place orders."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .observer import MarketBar, ObservationReport, observe_market
from .openai_advisor import AdvisorError, AdvisorResult, OpenAIAdvisor
from .ranking import RankedAsset
from .sessions import AssetSessionProfile
from .state import AuditStore
from .strategy import Decision


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ManualAnalysisResult:
    payload: dict[str, Any]
    analysis_id: int | None = None
    new_record: bool = True

    def as_json_dict(self) -> dict[str, Any]:
        result = dict(self.payload)
        if self.analysis_id is not None:
            result["id"] = self.analysis_id
        result["new_record"] = self.new_record
        return result


def _one(items: tuple[Any, ...], symbol: str) -> tuple[Any | None, int]:
    matches = [item for item in items if item.symbol == symbol]
    return (matches[0] if len(matches) == 1 else None, len(matches))


def _check(name: str, passed: bool, details: str) -> dict[str, str]:
    return {"name": name, "status": "PASS" if passed else "FAIL", "details": details[:500]}


def _base_verdict(decision: Decision | None) -> str:
    if decision is None:
        return "INCONCLUSIVE"
    if decision.signal == "EXIT_CANDIDATE":
        return "AVOID"
    return "WAIT"


def build_manual_analysis(
    report: ObservationReport,
    symbol: str,
    *,
    advisor: OpenAIAdvisor | None = None,
) -> dict[str, Any]:
    """Build one report from matching objects in one immutable observation."""
    normalized = symbol.strip().upper()
    top10 = tuple(report.ranking[:10])
    ranked, ranked_count = _one(top10, normalized)
    if ranked is None:
        raise ValueError("symbol is not in the fresh Top 10 ranking")
    decision, decision_count = _one(report.decisions, normalized)
    bar, bar_count = _one(report.market_bars, normalized)
    profile, profile_count = _one(report.session_profiles, normalized)
    assert isinstance(ranked, RankedAsset)

    checks = [
        _check("symbol_in_fresh_top10", ranked_count == 1 and 1 <= ranked.rank <= 10,
               f"rank={ranked.rank}; symbol={normalized}"),
        _check("unique_matching_decision", decision_count == 1, f"matches={decision_count}"),
        _check("unique_matching_market_bar", bar_count == 1, f"matches={bar_count}"),
        _check("unique_matching_session_profile", profile_count == 1, f"matches={profile_count}"),
        _check("strategy_version_matches", isinstance(decision, Decision) and decision.strategy_version == report.strategy_version,
               "decision/report strategy versions must match"),
        _check("candle_close_time_matches", isinstance(decision, Decision) and isinstance(bar, MarketBar)
               and decision.candle_close_time == bar.close_time,
               "decision and current market bar must share the closed candle"),
        _check("entry_candidate_has_risk_plan", not isinstance(decision, Decision)
               or decision.signal != "ENTRY_CANDIDATE" or decision.risk_plan is not None,
               "an ENTRY_CANDIDATE must include its deterministic quantity, stop and target"),
        _check("observation_is_non_execution", report.mode == "OBSERVATION",
               f"mode={report.mode}; orders_enabled=false"),
    ]
    anomalies = [item["name"] + ": " + item["details"] for item in checks if item["status"] == "FAIL"]
    if normalized in report.session_skipped_symbols:
        anomalies.append("session profile was skipped during fresh observation")

    ranking_json = ranked.as_json_dict()
    decision_json = decision.as_json_dict() if isinstance(decision, Decision) else None
    bar_json = bar.as_json_dict() if isinstance(bar, MarketBar) else None
    profile_json = profile.as_json_dict() if isinstance(profile, AssetSessionProfile) else None
    observation_payload = report.as_json_dict()
    snapshot_hash = payload_hash(observation_payload)
    ai_input = {
        "generated_at_ms": report.generated_at_ms,
        "symbol": normalized,
        "rank": ranked.rank,
        "ranking_metrics": ranking_json,
        "deterministic_decision": decision_json,
        "session_profile": profile_json,
        "current_market_bar": bar_json,
        "integrity_checks": checks,
        "anomalies": anomalies,
        "constraints": {"advisory_only": True, "orders_enabled": False, "may_not_change_risk_plan": True},
    }
    input_hash = payload_hash(ai_input)
    assessment: dict[str, Any] | None = None
    ai_status = "DISABLED"
    ai_error: str | None = None
    ai_model = advisor.model if advisor is not None else None
    response_hash: str | None = None
    all_pass = all(item["status"] == "PASS" for item in checks)
    if advisor is not None and all_pass:
        try:
            result: AdvisorResult = advisor.assess(ai_input)
            assessment = result.as_json_dict()
            response_hash = result.response_hash
            ai_status = "VALID"
        except AdvisorError as exc:
            ai_status = "INVALID"
            ai_error = str(exc)[:500]
            anomalies.append("AI assessment invalid: " + ai_error)
    elif advisor is not None:
        ai_status = "SKIPPED_INTEGRITY_FAILURE"

    if not all_pass or ai_status in {"INVALID", "SKIPPED_INTEGRITY_FAILURE"}:
        final_verdict = "INCONCLUSIVE"
    elif not isinstance(decision, Decision) or decision.signal != "ENTRY_CANDIDATE":
        final_verdict = _base_verdict(decision)
    elif ai_status != "VALID" or assessment is None:
        final_verdict = "INCONCLUSIVE"
    elif assessment["verdict"] == "OPPORTUNITY":
        final_verdict = "OPPORTUNITY"
    elif assessment["verdict"] in {"WAIT", "AVOID", "INCONCLUSIVE"}:
        final_verdict = assessment["verdict"]
    else:  # defensive fail-closed branch
        final_verdict = "INCONCLUSIVE"

    return {
        "generated_at_ms": report.generated_at_ms,
        "symbol": normalized,
        "rank": ranked.rank,
        "strategy_version": report.strategy_version,
        "candle_close_time": decision.candle_close_time if isinstance(decision, Decision) else (
            bar.close_time if isinstance(bar, MarketBar) else 0),
        "snapshot_hash": snapshot_hash,
        "ranking_metrics": ranking_json,
        "deterministic_decision": decision_json,
        "session_profile": profile_json,
        "current_market_bar": bar_json,
        "integrity_checks": checks,
        "anomalies": anomalies,
        "ai": {
            "status": ai_status,
            "model": ai_model,
            "input_hash": input_hash,
            "response_hash": response_hash,
            "assessment": assessment,
            "error": ai_error,
        },
        "final_verdict": final_verdict,
        "orders_enabled": False,
    }


def run_manual_analysis(
    config_path: str | Path,
    db_path: str | Path,
    symbol: str,
    *,
    advisor: OpenAIAdvisor | None = None,
    client: Any | None = None,
    testnet_client: Any | None = None,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Run and persist a fresh observation followed by one advisory report."""
    settings: Settings = load_settings(config_path)
    report = observe_market(settings, client=client, testnet_client=testnet_client, now_ms=now_ms)
    store = AuditStore(db_path)
    try:
        for decision in report.decisions:
            store.record(decision)
        store.record_report(report)
        payload = build_manual_analysis(report, symbol, advisor=advisor)
        analysis_id, inserted = store.record_manual_analysis(payload)
        result = dict(payload)
        result["id"] = analysis_id
        result["new_record"] = inserted
        return result
    finally:
        store.close()
