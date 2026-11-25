"""Deterministic Maxx Nexus review layer for PAPER entry candidates.

Phase 1 deliberately uses only evidence already produced by this repository.
Unavailable fundamental, news, macro, on-chain, and broader market feeds are
reported explicitly and are never converted into neutral or positive evidence.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any

from .config import Settings
from .probability import GateEvidence
from .strategy import Decision

SCHEMA_VERSION = 1
ROLE_CATALOG = {
    "INTELLIGENCE": (
        "PESQUISADOR_MERCADO",
        "ANALISTA_FUNDAMENTALISTA",
        "ANALISTA_TECNICO",
        "ANALISTA_QUANTITATIVO",
        "ANALISTA_ONCHAIN",
        "ANALISTA_MACRO",
        "ANALISTA_NOTICIAS",
        "ANALISTA_MERCADO",
    ),
    "CONTROL": (
        "ANALISTA_COMPLIANCE",
        "RISK_MANAGER",
        "ANALISTA_RISCO",
        "GESTOR_CAPITAL",
    ),
    "DESK": ("HEAD_TRADER", "SENIOR_TRADER", "JUNIOR_TRADER"),
    "DISABLED": ("SCALPER_TRADER",),
    "AUDIT": ("AUDITOR_FINANCEIRO",),
}


def _canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def validate_nexus_review(payload: dict[str, Any]) -> bool:
    """Verify content hashes for a persisted review and all analyst reports."""
    if not isinstance(payload, dict) or not isinstance(payload.get("review_hash"), str):
        return False
    unsigned = dict(payload)
    expected = unsigned.pop("review_hash")
    if _hash(unsigned) != expected:
        return False
    reports = payload.get("reports")
    if not isinstance(reports, list):
        return False
    for report in reports:
        if not isinstance(report, dict) or not isinstance(report.get("report_hash"), str):
            return False
        unsigned_report = dict(report)
        expected_report = unsigned_report.pop("report_hash")
        if _hash(unsigned_report) != expected_report:
            return False
    return True


def _report(role: str, status: str, recommendation: str, reason_codes: list[str], evidence: dict[str, Any]) -> dict[str, Any]:
    body = {
        "schema_version": SCHEMA_VERSION,
        "role": role,
        "status": status,
        "recommendation": recommendation,
        "reason_codes": reason_codes,
        "evidence": evidence,
    }
    body["report_hash"] = _hash(body)
    return body


def build_nexus_review(
    decision: Decision,
    evidence: GateEvidence,
    settings: Settings,
    config_hash: str,
    *,
    paused: bool,
    day_locked: bool,
    open_symbols: tuple[str, ...],
    open_count: int,
    cash: Decimal,
    allocation: Decimal,
    selected_by_probability: bool,
) -> dict[str, Any]:
    """Build one deterministic, side-effect-free candidate review."""
    reports: list[dict[str, Any]] = []
    technical_support = decision.signal == "ENTRY_CANDIDATE" and decision.risk_plan is not None
    reports.append(_report(
        "ANALISTA_TECNICO",
        "READY" if technical_support else "INVALID",
        "SUPPORT" if technical_support else "OPPOSE",
        ["TECHNICAL_ENTRY_VALID"] if technical_support else ["TECHNICAL_ENTRY_INVALID"],
        {"signal": decision.signal, "candle_close_time": decision.candle_close_time},
    ))
    probability_support = evidence.allowed and (
        settings.probability_gate_mode != "REQUIRED" or selected_by_probability
    )
    reports.append(_report(
        "ANALISTA_QUANTITATIVO",
        "READY" if evidence.status not in {"UNAVAILABLE"} else "UNAVAILABLE",
        "SUPPORT" if probability_support else "ABSTAIN",
        ["PROBABILITY_GATE_SUPPORT"] if probability_support else ["PROBABILITY_EVIDENCE_NOT_SUFFICIENT"],
        evidence.as_json_dict(),
    ))
    unavailable_roles = (
        "PESQUISADOR_MERCADO",
        "ANALISTA_FUNDAMENTALISTA",
        "ANALISTA_ONCHAIN",
        "ANALISTA_MACRO",
        "ANALISTA_NOTICIAS",
        "ANALISTA_MERCADO",
    )
    for role in unavailable_roles:
        reports.append(_report(
            role,
            "NOT_CONFIGURED",
            "ABSTAIN",
            ["EXTERNAL_SOURCE_NOT_CONFIGURED"],
            {"source": None, "treated_as_positive": False},
        ))

    compliance_pass = (
        settings.market_data_source == "BINANCE_PUBLIC_PRODUCTION"
        and settings.future_execution_target == "BINANCE_SPOT_TESTNET"
    )
    risk_plan = decision.risk_plan
    risk_pass = bool(
        risk_plan is not None
        and risk_plan.stop < risk_plan.entry < risk_plan.target
        and risk_plan.planned_position_usdt <= risk_plan.maximum_position_usdt
    )
    capital_pass = (
        allocation > 0
        and cash >= allocation
        and open_count < settings.max_positions
        and decision.symbol not in open_symbols
    )
    council_approve = technical_support and probability_support and not unavailable_roles
    gates = [
        {
            "name": "COMPLIANCE",
            "result": "PASS" if compliance_pass else "BLOCK",
            "reason_codes": ["PAPER_ONLY_PUBLIC_DATA"] if compliance_pass else ["EXECUTION_BOUNDARY_INVALID"],
        },
        {
            "name": "RISK",
            "result": "PASS" if risk_pass and not day_locked else "BLOCK",
            "reason_codes": ["RISK_PLAN_VALID"] if risk_pass and not day_locked else ["RISK_PLAN_OR_DAILY_LOCK_BLOCK"],
        },
        {
            "name": "CAPITAL",
            "result": "PASS" if capital_pass else "BLOCK",
            "reason_codes": ["CAPITAL_AVAILABLE"] if capital_pass else ["CAPITAL_OR_CAPACITY_BLOCK"],
        },
        {
            "name": "TRADING_DESK",
            "result": "PASS" if council_approve and not paused else "BLOCK",
            "reason_codes": ["COUNCIL_APPROVED"] if council_approve and not paused else ["COUNCIL_ABSTAIN_OR_PAUSED"],
        },
    ]
    permit_eligible = all(gate["result"] == "PASS" for gate in gates)
    reason_codes = []
    if unavailable_roles:
        reason_codes.append("EXTERNAL_CONTEXT_NOT_CONFIGURED")
    if not probability_support:
        reason_codes.append("PROBABILITY_EVIDENCE_NOT_SUFFICIENT")
    reason_codes.extend(
        code for gate in gates if gate["result"] != "PASS" for code in gate["reason_codes"]
    )
    if settings.nexus_mode == "AUDIT":
        disposition = "SHADOW_ALLOW" if permit_eligible else "SHADOW_BLOCK"
    elif settings.nexus_mode == "ENFORCE":
        disposition = "PERMIT_ELIGIBLE" if permit_eligible else "BLOCKED"
    else:
        disposition = "NOT_APPLICABLE"
    review: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "system": "MAXX_NEXUS",
        "mode": settings.nexus_mode,
        "candidate_key": f"{settings.strategy_version}:{decision.candle_close_time}:{decision.symbol}",
        "symbol": decision.symbol,
        "candle_close_time": decision.candle_close_time,
        "strategy_version": settings.strategy_version,
        "config_hash": config_hash,
        "reports": reports,
        "council": {
            "verdict": "APPROVE" if council_approve else "ABSTAIN",
            "reason_codes": ["ALL_REQUIRED_CONTEXT_READY"] if council_approve else ["REQUIRED_CONTEXT_UNAVAILABLE"],
        },
        "gates": gates,
        "disposition": disposition,
        "reason_codes": sorted(set(reason_codes)),
        "paper_only": True,
        "orders_enabled": False,
    }
    review["review_hash"] = _hash(review)
    return review
