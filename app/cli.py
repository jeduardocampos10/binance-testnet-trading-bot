"""Command-line interface for one-shot observation and local status."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from .config import ConfigurationError
from .market_data import MarketDataError
from .models import DataValidationError
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

        store.close()
        store = None
        payload = run_analysis_cycle(args.config, args.db, paper=args.command == "paper")
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    except (ConfigurationError, MarketDataError, DataValidationError, ValueError, OSError, sqlite3.Error) as exc:
        print(f"observer failed safely: {exc}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
