"""SQLite audit store for observation decisions."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import DAY_MS
from .observer import ObservationReport
from .strategy import Decision


class AuditStore:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self._path, timeout=30)
        self._paper_transaction = False
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA busy_timeout=30000")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS decisions (
                id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                candle_close_time INTEGER NOT NULL,
                signal TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, strategy_version, candle_close_time)
            )
            """
        )
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS observation_reports (
                id INTEGER PRIMARY KEY,
                generated_at_ms INTEGER NOT NULL,
                strategy_version TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(generated_at_ms, strategy_version)
            )
            """
        )
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                initial_equity TEXT NOT NULL,
                cash TEXT NOT NULL,
                config_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_positions (
                id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                entry_day TEXT NOT NULL,
                entry_time_ms INTEGER NOT NULL,
                entry_candle_close_time INTEGER,
                entry_price TEXT NOT NULL,
                quantity TEXT NOT NULL,
                entry_fee TEXT NOT NULL,
                hard_stop TEXT NOT NULL,
                trailing_stop TEXT,
                peak_price TEXT NOT NULL,
                last_mark TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('OPEN', 'CLOSED')),
                exit_time_ms INTEGER,
                exit_price TEXT,
                exit_fee TEXT,
                exit_reason TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_paper_position_per_symbol
                ON paper_positions(symbol) WHERE status = 'OPEN';
            CREATE TABLE IF NOT EXISTS paper_days (
                day_utc TEXT PRIMARY KEY,
                start_equity TEXT NOT NULL,
                locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
                lock_reason TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_events (
                id INTEGER PRIMARY KEY,
                event_key TEXT NOT NULL UNIQUE,
                day_utc TEXT NOT NULL,
                generated_at_ms INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                symbol TEXT,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS paper_reports (
                id INTEGER PRIMARY KEY,
                generated_at_ms INTEGER NOT NULL UNIQUE,
                day_utc TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS paper_cycles (
                candle_close_time INTEGER PRIMARY KEY,
                strategy_version TEXT NOT NULL,
                config_hash TEXT,
                generated_at_ms INTEGER NOT NULL,
                completed INTEGER NOT NULL DEFAULT 0 CHECK (completed IN (0, 1)),
                payload_json TEXT
            );
            CREATE TABLE IF NOT EXISTS paper_control (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                paused INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0, 1)),
                reason TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT OR IGNORE INTO paper_control (id, paused) VALUES (1, 0);
            """
        )
        account_columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(paper_account)").fetchall()
        }
        if "config_hash" not in account_columns:
            self._connection.execute("ALTER TABLE paper_account ADD COLUMN config_hash TEXT")
        cycle_columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(paper_cycles)").fetchall()
        }
        if "config_hash" not in cycle_columns:
            self._connection.execute("ALTER TABLE paper_cycles ADD COLUMN config_hash TEXT")
        position_columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(paper_positions)").fetchall()
        }
        if "entry_candle_close_time" not in position_columns:
            self._connection.execute(
                "ALTER TABLE paper_positions ADD COLUMN entry_candle_close_time INTEGER"
            )
        self._connection.commit()

    def _commit(self) -> None:
        if not self._paper_transaction:
            self._connection.commit()

    def begin_paper_cycle(self, candle_close_time: int, strategy_version: str, config_hash: str,
                          generated_at_ms: int) -> dict[str, Any] | None:
        """Acquire the global PAPER write lock and claim one market candle."""
        self._connection.execute("BEGIN IMMEDIATE")
        self._paper_transaction = True
        existing = self._connection.execute(
            """SELECT completed, payload_json, strategy_version, config_hash
               FROM paper_cycles WHERE candle_close_time = ?""",
            (candle_close_time,),
        ).fetchone()
        if existing is not None:
            self._connection.rollback()
            self._paper_transaction = False
            if existing[2] != strategy_version or existing[3] != config_hash:
                raise ValueError("PAPER candle was already processed with another configuration")
            if existing[0] and existing[1] is not None:
                return json.loads(existing[1])
            raise ValueError("PAPER cycle is already being processed")
        latest = self._connection.execute(
            "SELECT MAX(candle_close_time) FROM paper_cycles WHERE completed = 1"
        ).fetchone()[0]
        if latest is not None and candle_close_time < latest:
            self._connection.rollback()
            self._paper_transaction = False
            raise ValueError("stale or out-of-order PAPER candle")
        has_open_positions = self._connection.execute(
            "SELECT 1 FROM paper_positions WHERE status = 'OPEN' LIMIT 1"
        ).fetchone() is not None
        if has_open_positions and (latest is None or candle_close_time != latest + DAY_MS):
            self._connection.rollback()
            self._paper_transaction = False
            raise ValueError("non-contiguous PAPER candle while positions are open")
        self._connection.execute(
            """INSERT INTO paper_cycles
                   (candle_close_time, strategy_version, config_hash, generated_at_ms, completed)
               VALUES (?, ?, ?, ?, 0)""",
            (candle_close_time, strategy_version, config_hash, generated_at_ms),
        )
        return None

    def complete_paper_cycle(self, candle_close_time: int, payload: dict[str, Any]) -> None:
        if not self._paper_transaction:
            raise ValueError("no PAPER cycle transaction is active")
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        cursor = self._connection.execute(
            """UPDATE paper_cycles SET completed = 1, payload_json = ?
               WHERE candle_close_time = ? AND completed = 0""",
            (text, candle_close_time),
        )
        if cursor.rowcount != 1:
            self.abort_paper_cycle()
            raise ValueError("unable to complete PAPER cycle")
        self._connection.commit()
        self._paper_transaction = False

    def abort_paper_cycle(self) -> None:
        if self._paper_transaction:
            self._connection.rollback()
            self._paper_transaction = False

    def close(self) -> None:
        self.abort_paper_cycle()
        self._connection.close()

    def record(self, decision: Decision) -> bool:
        payload = json.dumps(decision.as_json_dict(), sort_keys=True, separators=(",", ":"))
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO decisions
                (symbol, strategy_version, candle_close_time, signal, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (decision.symbol, decision.strategy_version, decision.candle_close_time, decision.signal, payload),
        )
        self._commit()
        return cursor.rowcount == 1

    def record_report(self, report: ObservationReport) -> bool:
        payload = json.dumps(report.as_json_dict(), sort_keys=True, separators=(",", ":"))
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO observation_reports
                (generated_at_ms, strategy_version, payload_json)
            VALUES (?, ?, ?)
            """,
            (report.generated_at_ms, report.strategy_version, payload),
        )
        self._commit()
        return cursor.rowcount == 1

    def latest_report(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json, recorded_at FROM observation_reports ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        payload["recorded_at"] = row[1]
        return payload

    def latest(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json, recorded_at FROM decisions ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        payload["recorded_at"] = row[1]
        return payload

    def ensure_paper_account(self, initial_equity: str, config_hash: str) -> None:
        self._connection.execute(
            """INSERT OR IGNORE INTO paper_account
                   (id, initial_equity, cash, config_hash) VALUES (1, ?, ?, ?)""",
            (initial_equity, initial_equity, config_hash),
        )
        row = self._connection.execute(
            "SELECT config_hash FROM paper_account WHERE id = 1"
        ).fetchone()
        if row is None or row[0] != config_hash:
            raise ValueError("PAPER database configuration hash mismatch; use a new database")
        self._commit()

    def paper_account(self) -> dict[str, str]:
        row = self._connection.execute(
            "SELECT initial_equity, cash, config_hash FROM paper_account WHERE id = 1"
        ).fetchone()
        if row is None:
            raise ValueError("PAPER account is not initialized")
        return {"initial_equity": row[0], "cash": row[1], "config_hash": row[2]}

    def open_positions(self) -> list[dict[str, Any]]:
        columns = (
            "id", "symbol", "entry_day", "entry_time_ms", "entry_candle_close_time",
            "entry_price", "quantity", "entry_fee", "hard_stop", "trailing_stop",
            "peak_price", "last_mark",
        )
        rows = self._connection.execute(
            """SELECT id, symbol, entry_day, entry_time_ms, entry_candle_close_time,
                      entry_price, quantity, entry_fee, hard_stop, trailing_stop, peak_price, last_mark
               FROM paper_positions WHERE status = 'OPEN' ORDER BY id"""
        ).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]

    def open_symbols(self) -> tuple[str, ...]:
        return tuple(position["symbol"] for position in self.open_positions())

    def ensure_paper_day(self, day_utc: str, start_equity: str) -> None:
        self._connection.execute(
            "INSERT OR IGNORE INTO paper_days (day_utc, start_equity) VALUES (?, ?)",
            (day_utc, start_equity),
        )
        self._commit()

    def paper_day(self, day_utc: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT start_equity, locked, lock_reason FROM paper_days WHERE day_utc = ?",
            (day_utc,),
        ).fetchone()
        if row is None:
            raise ValueError("PAPER day is not initialized")
        return {"start_equity": row[0], "locked": bool(row[1]), "lock_reason": row[2]}

    def lock_paper_day(self, day_utc: str, reason: str) -> None:
        self._connection.execute(
            "UPDATE paper_days SET locked = 1, lock_reason = ? WHERE day_utc = ?",
            (reason, day_utc),
        )
        self._commit()

    def paper_entry_exists(self, symbol: str, day_utc: str) -> bool:
        row = self._connection.execute(
            "SELECT 1 FROM paper_positions WHERE symbol = ? AND entry_day = ? LIMIT 1",
            (symbol, day_utc),
        ).fetchone()
        return row is not None

    def open_paper_position(self, position: dict[str, Any], event: dict[str, Any], new_cash: str) -> bool:
        payload = json.dumps(event["payload"], sort_keys=True, separators=(",", ":"))
        self._connection.execute(
            """INSERT INTO paper_events
                   (event_key, day_utc, generated_at_ms, event_type, symbol, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event["event_key"], event["day_utc"], event["generated_at_ms"], event["event_type"], event["symbol"], payload),
        )
        self._connection.execute(
            """INSERT INTO paper_positions
                   (symbol, entry_day, entry_time_ms, entry_candle_close_time,
                    entry_price, quantity, entry_fee, hard_stop, trailing_stop,
                    peak_price, last_mark, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
            (
                position["symbol"], position["entry_day"], position["entry_time_ms"],
                position["entry_candle_close_time"], position["entry_price"],
                position["quantity"], position["entry_fee"], position["hard_stop"],
                position["trailing_stop"], position["peak_price"], position["last_mark"],
            ),
        )
        self._connection.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (new_cash,))
        self._commit()
        return True

    def update_paper_position(self, position_id: int, *, peak_price: str, trailing_stop: str | None, last_mark: str) -> None:
        self._connection.execute(
            """UPDATE paper_positions SET peak_price = ?, trailing_stop = ?, last_mark = ?
               WHERE id = ? AND status = 'OPEN'""",
            (peak_price, trailing_stop, last_mark, position_id),
        )
        self._commit()

    def close_paper_position(self, position_id: int, *, exit_time_ms: int, exit_price: str, exit_fee: str,
                             exit_reason: str, event: dict[str, Any], new_cash: str) -> bool:
        payload = json.dumps(event["payload"], sort_keys=True, separators=(",", ":"))
        cursor = self._connection.execute(
            """UPDATE paper_positions SET status = 'CLOSED', exit_time_ms = ?, exit_price = ?,
                      exit_fee = ?, exit_reason = ?, last_mark = ?
               WHERE id = ? AND status = 'OPEN'""",
            (exit_time_ms, exit_price, exit_fee, exit_reason, exit_price, position_id),
        )
        if cursor.rowcount != 1:
            raise sqlite3.IntegrityError("position already closed")
        self._connection.execute(
            """INSERT INTO paper_events
                   (event_key, day_utc, generated_at_ms, event_type, symbol, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event["event_key"], event["day_utc"], event["generated_at_ms"], event["event_type"], event["symbol"], payload),
        )
        self._connection.execute("UPDATE paper_account SET cash = ? WHERE id = 1", (new_cash,))
        self._commit()
        return True

    def record_paper_event(self, event: dict[str, Any]) -> bool:
        payload = json.dumps(event["payload"], sort_keys=True, separators=(",", ":"))
        cursor = self._connection.execute(
            """INSERT OR IGNORE INTO paper_events
                   (event_key, day_utc, generated_at_ms, event_type, symbol, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event["event_key"], event["day_utc"], event["generated_at_ms"], event["event_type"], event["symbol"], payload),
        )
        self._commit()
        return cursor.rowcount == 1

    def paper_events_for_day(self, day_utc: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT event_type, symbol, payload_json, generated_at_ms
               FROM paper_events WHERE day_utc = ? ORDER BY id""",
            (day_utc,),
        ).fetchall()
        return [
            {"event_type": row[0], "symbol": row[1], "payload": json.loads(row[2]), "generated_at_ms": row[3]}
            for row in rows
        ]

    def record_paper_report(self, generated_at_ms: int, day_utc: str, payload: dict[str, Any]) -> bool:
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        cursor = self._connection.execute(
            "INSERT OR IGNORE INTO paper_reports (generated_at_ms, day_utc, payload_json) VALUES (?, ?, ?)",
            (generated_at_ms, day_utc, text),
        )
        self._commit()
        return cursor.rowcount == 1

    def latest_paper_report(self) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT payload_json, recorded_at FROM paper_reports ORDER BY generated_at_ms DESC, id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        payload["recorded_at"] = row[1]
        return payload

    def paper_control(self) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT paused, reason, updated_at FROM paper_control WHERE id = 1"
        ).fetchone()
        if row is None:
            raise ValueError("PAPER control state is unavailable")
        return {"paused": bool(row[0]), "reason": row[1], "updated_at": row[2]}

    def set_paper_paused(self, paused: bool, reason: str | None) -> None:
        clean_reason = reason.strip()[:200] if isinstance(reason, str) and reason.strip() else None
        self._connection.execute(
            """UPDATE paper_control SET paused = ?, reason = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = 1""",
            (1 if paused else 0, clean_reason),
        )
        self._commit()

    def paper_reports_since(self, since_ms: int | None = None) -> list[dict[str, Any]]:
        if since_ms is None:
            rows = self._connection.execute(
                "SELECT payload_json FROM paper_reports ORDER BY generated_at_ms"
            ).fetchall()
        else:
            rows = self._connection.execute(
                """SELECT payload_json FROM paper_reports
                   WHERE generated_at_ms >= ? ORDER BY generated_at_ms""",
                (since_ms,),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def closed_paper_positions(self, since_ms: int | None = None) -> list[dict[str, Any]]:
        columns = (
            "symbol", "entry_time_ms", "entry_price", "quantity", "entry_fee",
            "exit_time_ms", "exit_price", "exit_fee", "exit_reason",
        )
        query = """SELECT symbol, entry_time_ms, entry_price, quantity, entry_fee,
                          exit_time_ms, exit_price, exit_fee, exit_reason
                   FROM paper_positions WHERE status = 'CLOSED'"""
        params: tuple[Any, ...] = ()
        if since_ms is not None:
            query += " AND exit_time_ms >= ?"
            params = (since_ms,)
        query += " ORDER BY exit_time_ms"
        rows = self._connection.execute(query, params).fetchall()
        return [dict(zip(columns, row, strict=True)) for row in rows]
