"""Application service functions shared by CLI and the authenticated dashboard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import load_settings
from .observer import observe_market
from .paper import run_paper_cycle
from .state import AuditStore


def run_analysis_cycle(config_path: str | Path, db_path: str | Path, *, paper: bool) -> dict[str, Any]:
    settings = load_settings(config_path)
    store = AuditStore(db_path)
    try:
        required_symbols = store.open_symbols() if paper else ()
        report = observe_market(settings, required_symbols=required_symbols)
        for decision in report.decisions:
            store.record(decision)
        inserted = store.record_report(report)
        payload = run_paper_cycle(report, settings, store) if paper else report.as_json_dict()
        payload["new_record"] = inserted
        return payload
    finally:
        store.close()
