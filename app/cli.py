"""Command-line interface for one-shot observation and local status."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from .config import ConfigurationError, load_settings, settings_fingerprint
from .market_data import MarketDataError
from .models import DataValidationError
from .probability import ProbabilityModelError, fit_artifact, read_artifact, write_artifact
from .service import run_analysis_cycle
from .state import AuditStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Binance Spot Testnet public-data analyzer and PAPER simulator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    observe = subparsers.add_parser("observe", help="fetch public closed candles and record analysis only")
    observe.add_argument("--config", required=True)
    observe.add_argument("--db", required=True)
    paper = subparsers.add_parser("paper", help="run one public-data PAPER portfolio cycle")
    paper.add_argument("--config", required=True)
    paper.add_argument("--db", required=True)
    status = subparsers.add_parser("status", help="show the most recent PAPER or observation report")
    status.add_argument("--db", required=True)
    probability_fit = subparsers.add_parser("probability-fit", help="fit one local calibrated probability artifact")
    probability_fit.add_argument("--config", required=True)
    probability_fit.add_argument("--db", required=True)
    probability_fit.add_argument("--artifact", required=True)
    probability_status = subparsers.add_parser("probability-status", help="show collection and optional artifact status")
    probability_status.add_argument("--db", required=True)
    probability_status.add_argument("--config")
    probability_status.add_argument("--artifact")
    nexus_status = subparsers.add_parser("nexus-status", help="show read-only Maxx Nexus reviews and PAPER permits")
    nexus_status.add_argument("--db", required=True)
    nexus_status.add_argument("--symbol")
    nexus_status.add_argument("--limit", type=int, default=20)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    store: AuditStore | None = None
    try:
        store = AuditStore(args.db)
        if args.command == "status":
            payload = store.latest_paper_report() or store.latest_report() or store.latest()
            print(json.dumps(payload or {"mode": "OBSERVATION", "status": "no decisions recorded"}, indent=2, sort_keys=True))
            return 0
        if args.command == "nexus-status":
            print(json.dumps(store.nexus_status(args.symbol, limit=args.limit), indent=2, sort_keys=True))
            return 0
        if args.command == "probability-fit":
            settings = load_settings(args.config)
            rows = store.matured_probability_candidates(
                settings.strategy_version, settings_fingerprint(settings)
            )
            artifact = fit_artifact(rows, settings)
            write_artifact(args.artifact, artifact)
            print(json.dumps(artifact, indent=2, sort_keys=True))
            return 0
        if args.command == "probability-status":
            payload = store.probability_status()
            if args.artifact or args.config:
                if not (args.artifact and args.config):
                    raise ValueError("--artifact and --config must be supplied together")
                settings = load_settings(args.config)
                try:
                    artifact = read_artifact(args.artifact, settings)
                    payload["artifact"] = {
                        "status": artifact["status"],
                        "artifact_hash": artifact["artifact_hash"],
                        "sample_counts": artifact["sample_counts"],
                        "metrics": artifact.get("metrics"),
                    }
                except ProbabilityModelError as exc:
                    payload["artifact"] = {"status": "UNAVAILABLE", "reason": str(exc)}
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0

        store.close()
        store = None
        payload = run_analysis_cycle(args.config, args.db, paper=args.command == "paper")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (ConfigurationError, MarketDataError, DataValidationError, ProbabilityModelError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"observer failed safely: {exc}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
