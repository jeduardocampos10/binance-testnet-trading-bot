"""Strictly read-only projections for the unified PAPER dashboard."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


class DashboardReadStore:
    """Read PAPER state without creating tables, directories, or write locks."""

    def __init__(self, path: str | Path) -> None:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise sqlite3.OperationalError("PAPER database is unavailable")
        uri = f"{resolved.as_uri()}?mode=ro"
        self._connection = sqlite3.connect(uri, uri=True, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA query_only=ON")
        self._connection.execute("PRAGMA busy_timeout=5000")

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "DashboardReadStore":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    @staticmethod
    def _limit(value: int) -> int:
        if not 1 <= value <= 200:
            raise ValueError("limit must be between 1 and 200")
        return value

    @staticmethod
    def _payload(text: str) -> dict[str, Any]:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError("stored PAPER event is invalid") from None
        if not isinstance(payload, dict):
            raise ValueError("stored PAPER event is invalid")
        return payload

    @staticmethod
    def _decimal(value: object) -> Decimal:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError("stored financial value is invalid") from None
        if not result.is_finite():
            raise ValueError("stored financial value is invalid")
        return result

    @staticmethod
    def _text(value: Decimal) -> str:
        return format(value, "f")

    def events(
        self,
        *,
        limit: int = 100,
        before_id: int | None = None,
        symbol: str | None = None,
        event_type: str | None = None,
    ) -> dict[str, Any]:
        limit = self._limit(limit)
        clauses: list[str] = []
        parameters: list[object] = []
        if before_id is not None:
            if before_id < 1:
                raise ValueError("before_id must be positive")
            clauses.append("id < ?")
            parameters.append(before_id)
        if symbol is not None:
            clauses.append("symbol = ?")
            parameters.append(symbol)
        if event_type is not None:
            clauses.append("event_type = ?")
            parameters.append(event_type)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit + 1)
        rows = self._connection.execute(
            f"""SELECT id, event_key, day_utc, generated_at_ms, event_type, symbol, payload_json
                FROM paper_events {where} ORDER BY id DESC LIMIT ?""",
            parameters,
        ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = [
            {
                "id": row["id"],
                "event_key": row["event_key"],
                "day_utc": row["day_utc"],
                "occurred_at_ms": row["generated_at_ms"],
                "event_type": row["event_type"],
                "symbol": row["symbol"],
                "payload": self._payload(row["payload_json"]),
                "execution_mode": "PAPER",
            }
            for row in selected
        ]
        return {
            "schema_version": "paper.events.v1",
            "execution_mode": "PAPER",
            "orders_enabled": False,
            "items": items,
            "next_before_id": int(selected[-1]["id"]) if has_more and selected else None,
        }

    def orders(self, *, limit: int = 100, before_id: int | None = None) -> dict[str, Any]:
        limit = self._limit(limit)
        clauses = ["event_type IN ('PAPER_ENTRY', 'PAPER_EXIT')"]
        parameters: list[object] = []
        if before_id is not None:
            if before_id < 1:
                raise ValueError("before_id must be positive")
            clauses.append("id < ?")
            parameters.append(before_id)
        parameters.append(limit + 1)
        rows = self._connection.execute(
            f"""SELECT id, generated_at_ms, event_type, symbol, payload_json
                FROM paper_events WHERE {' AND '.join(clauses)} ORDER BY id DESC LIMIT ?""",
            parameters,
        ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        items: list[dict[str, Any]] = []
        for row in selected:
            payload = self._payload(row["payload_json"])
            is_entry = row["event_type"] == "PAPER_ENTRY"
            items.append(
                {
                    "id": f"paper-event:{row['id']}",
                    "event_id": row["id"],
                    "symbol": row["symbol"],
                    "side": "BUY" if is_entry else "SELL",
                    "status": "FILLED_SIMULATED",
                    "event_type": row["event_type"],
                    "occurred_at_ms": row["generated_at_ms"],
                    "reference_price": payload.get("reference_price"),
                    "fill_price": payload.get("fill_price"),
                    "quantity": payload.get("quantity"),
                    "fee_usdt": payload.get("fee_usdt"),
                    "gross_usdt": payload.get("gross_usdt"),
                    "realized_pnl_usdt": payload.get("realized_pnl_usdt"),
                    "reason": payload.get("reason"),
                    "execution_mode": "PAPER",
                    "sent_to_binance": False,
                }
            )
        return {
            "schema_version": "paper.orders.v1",
            "execution_mode": "PAPER",
            "orders_enabled": False,
            "notice": "SIMULADA — NÃO ENVIADA À BINANCE",
            "items": items,
            "next_before_id": int(selected[-1]["id"]) if has_more and selected else None,
        }

    def positions(self, *, status: str = "open", fee_per_side_pct: object = "0") -> dict[str, Any]:
        normalized = status.lower()
        if normalized not in {"open", "closed", "all"}:
            raise ValueError("status must be open, closed, or all")
        where = "" if normalized == "all" else "WHERE status = ?"
        parameters: tuple[object, ...] = () if normalized == "all" else (normalized.upper(),)
        rows = self._connection.execute(
            f"""SELECT id, symbol, entry_day, entry_time_ms, entry_price, quantity, entry_fee,
                       hard_stop, trailing_stop, peak_price, last_mark, status, exit_time_ms,
                       exit_price, exit_fee, exit_reason
                FROM paper_positions {where} ORDER BY id DESC LIMIT 200""",
            parameters,
        ).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            quantity = self._decimal(row["quantity"])
            entry = self._decimal(row["entry_price"])
            mark = self._decimal(row["last_mark"])
            entry_fee = self._decimal(row["entry_fee"])
            exposure = quantity * mark
            exit_fee_rate = self._decimal(fee_per_side_pct) / Decimal("100")
            estimated_exit_fee = exposure * exit_fee_rate
            unrealized = quantity * (mark - entry) - entry_fee - estimated_exit_fee
            active_stop_raw = row["trailing_stop"] or row["hard_stop"]
            active_stop = self._decimal(active_stop_raw)
            risk_to_stop = max(Decimal("0"), quantity * (mark - active_stop))
            item = {
                "id": f"paper-position:{row['id']}",
                "position_id": row["id"],
                "symbol": row["symbol"],
                "status": row["status"],
                "entry_day": row["entry_day"],
                "entry_time_ms": row["entry_time_ms"],
                "entry_price": row["entry_price"],
                "quantity": row["quantity"],
                "entry_fee_usdt": row["entry_fee"],
                "mark_price": row["last_mark"],
                "hard_stop": row["hard_stop"],
                "trailing_stop": row["trailing_stop"],
                "active_stop": active_stop_raw,
                "peak_price": row["peak_price"],
                "exposure_usdt": self._text(exposure),
                "unrealized_pnl_usdt": self._text(unrealized),
                "risk_to_stop_usdt": self._text(risk_to_stop),
                "execution_mode": "PAPER",
                "sent_to_binance": False,
            }
            if row["status"] == "CLOSED":
                exit_price = self._decimal(row["exit_price"])
                exit_fee = self._decimal(row["exit_fee"])
                item.update(
                    {
                        "exit_time_ms": row["exit_time_ms"],
                        "exit_price": row["exit_price"],
                        "exit_fee_usdt": row["exit_fee"],
                        "exit_reason": row["exit_reason"],
                        "realized_pnl_usdt": self._text(quantity * (exit_price - entry) - entry_fee - exit_fee),
                    }
                )
            items.append(item)
        return {
            "schema_version": "paper.positions.v1",
            "execution_mode": "PAPER",
            "orders_enabled": False,
            "status_filter": normalized,
            "items": items,
        }

    def risk(
        self,
        *,
        max_positions: int,
        max_exposure_pct: object,
        daily_loss_limit_pct: object,
        fee_per_side_pct: object = "0",
    ) -> dict[str, Any]:
        self._connection.execute("BEGIN")
        account_columns = {
            str(row["name"])
            for row in self._connection.execute("PRAGMA table_info(paper_account)").fetchall()
        }
        optional_account_columns = [name for name in ("config_hash", "nexus_mode") if name in account_columns]
        selected_account_columns = ["initial_equity", "cash", *optional_account_columns]
        account = self._connection.execute(
            f"SELECT {', '.join(selected_account_columns)} FROM paper_account WHERE id = 1"
        ).fetchone()
        control = self._connection.execute(
            "SELECT paused, reason, updated_at FROM paper_control WHERE id = 1"
        ).fetchone()
        latest_report = self._connection.execute(
            "SELECT generated_at_ms, payload_json FROM paper_reports ORDER BY generated_at_ms DESC, id DESC LIMIT 1"
        ).fetchone()
        latest_day = self._connection.execute(
            "SELECT day_utc, start_equity, locked, lock_reason FROM paper_days ORDER BY day_utc DESC LIMIT 1"
        ).fetchone()
        positions = self.positions(status="open", fee_per_side_pct=fee_per_side_pct)["items"]
        exposure = sum((self._decimal(item["exposure_usdt"]) for item in positions), Decimal("0"))
        report = self._payload(latest_report["payload_json"]) if latest_report else {}
        result = {
            "schema_version": "paper.risk.v1",
            "as_of_ms": latest_report["generated_at_ms"] if latest_report else None,
            "execution_mode": "PAPER",
            "orders_enabled": False,
            "initial_equity_usdt": account["initial_equity"] if account else None,
            "equity_usdt": report.get("equity_usdt"),
            "cash_usdt": account["cash"] if account else report.get("cash_usdt"),
            "open_positions": len(positions),
            "max_positions": max_positions,
            "gross_exposure_usdt": self._text(exposure),
            "max_exposure_per_position_pct": str(max_exposure_pct),
            "daily_pnl_usdt": report.get("daily_pnl_usdt"),
            "daily_pnl_pct": report.get("daily_pnl_pct"),
            "daily_loss_limit_pct": str(daily_loss_limit_pct),
            "daily_locked": bool(latest_day["locked"]) if latest_day else bool(report.get("daily_lock", False)),
            "daily_lock_reason": latest_day["lock_reason"] if latest_day else None,
            "entries_paused": bool(control["paused"]) if control else False,
            "pause_reason": control["reason"] if control else None,
            "control_updated_at": control["updated_at"] if control else None,
            "configuration_hash": account["config_hash"] if account and "config_hash" in account.keys() else None,
            "nexus_mode": account["nexus_mode"] if account and "nexus_mode" in account.keys() else None,
            "probability_gate_mode": report.get("probability_gate_mode"),
            "risk_day_utc": latest_day["day_utc"] if latest_day else report.get("risk_day_utc"),
        }
        self._connection.rollback()
        return result
