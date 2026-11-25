"""SQLite audit store for observation decisions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import DAY_MS
from .nexus import validate_nexus_review
from .observer import ObservationReport
from .strategy import Decision


def _payload_hash(payload: dict[str, Any], hash_key: str) -> str:
    unsigned = dict(payload)
    unsigned.pop(hash_key, None)
    text = json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS manual_analyses (
                id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                strategy_version TEXT NOT NULL,
                candle_close_time INTEGER NOT NULL,
                snapshot_hash TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, strategy_version, candle_close_time, snapshot_hash)
            )
            """
        )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS manual_analyses_symbol_id ON manual_analyses(symbol, id DESC)"
        )
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                initial_equity TEXT NOT NULL,
                cash TEXT NOT NULL,
                config_hash TEXT,
                nexus_mode TEXT NOT NULL DEFAULT 'OFF'
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
            CREATE TABLE IF NOT EXISTS probability_market_bars (
                symbol TEXT NOT NULL,
                candle_close_time INTEGER NOT NULL,
                close TEXT NOT NULL,
                PRIMARY KEY (symbol, candle_close_time)
            );
            CREATE TABLE IF NOT EXISTS probability_candidates (
                id INTEGER PRIMARY KEY,
                symbol TEXT NOT NULL,
                candle_close_time INTEGER NOT NULL,
                strategy_version TEXT NOT NULL,
                config_hash TEXT NOT NULL,
                entry_close TEXT NOT NULL,
                features_json TEXT NOT NULL,
                label INTEGER CHECK (label IN (0, 1)),
                outcome_close_time INTEGER,
                net_return TEXT,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                labeled_at TEXT,
                UNIQUE(symbol, candle_close_time, strategy_version, config_hash)
            );
            CREATE INDEX IF NOT EXISTS probability_candidates_time
                ON probability_candidates(candle_close_time, symbol);
            CREATE TABLE IF NOT EXISTS nexus_reviews (
                id INTEGER PRIMARY KEY,
                candidate_key TEXT NOT NULL UNIQUE,
                symbol TEXT NOT NULL,
                candle_close_time INTEGER NOT NULL,
                mode TEXT NOT NULL CHECK (mode IN ('AUDIT', 'ENFORCE')),
                disposition TEXT NOT NULL,
                review_hash TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS nexus_reviews_symbol_id
                ON nexus_reviews(symbol, id DESC);
            CREATE TABLE IF NOT EXISTS paper_permits (
                id INTEGER PRIMARY KEY,
                review_id INTEGER NOT NULL UNIQUE REFERENCES nexus_reviews(id),
                symbol TEXT NOT NULL,
                candle_close_time INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('ISSUED', 'CONSUMED')),
                consumed_position_id INTEGER,
                payload_json TEXT NOT NULL,
                issued_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                consumed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS post_trade_audits (
                id INTEGER PRIMARY KEY,
                position_id INTEGER NOT NULL UNIQUE REFERENCES paper_positions(id),
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        account_columns = {
            row[1] for row in self._connection.execute("PRAGMA table_info(paper_account)").fetchall()
        }
        if "config_hash" not in account_columns:
            self._connection.execute("ALTER TABLE paper_account ADD COLUMN config_hash TEXT")
        if "nexus_mode" not in account_columns:
            self._connection.execute(
                "ALTER TABLE paper_account ADD COLUMN nexus_mode TEXT NOT NULL DEFAULT 'OFF'"
            )
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
        if "nexus_permit_id" not in position_columns:
            self._connection.execute(
                "ALTER TABLE paper_positions ADD COLUMN nexus_permit_id INTEGER REFERENCES paper_permits(id)"
            )
        self._connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS one_position_per_nexus_permit
               ON paper_positions(nexus_permit_id) WHERE nexus_permit_id IS NOT NULL"""
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

    def record_manual_analysis(self, payload: dict[str, Any]) -> tuple[int, bool]:
        """Persist canonical JSON and return (stable id, newly inserted)."""
        import hashlib
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        key = (
            payload["symbol"], payload["strategy_version"],
            int(payload["candle_close_time"]), payload["snapshot_hash"],
        )
        cursor = self._connection.execute(
            """INSERT OR IGNORE INTO manual_analyses
                   (symbol, strategy_version, candle_close_time, snapshot_hash, payload_hash, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (*key, digest, text),
        )
        inserted = cursor.rowcount == 1
        if inserted:
            analysis_id = int(cursor.lastrowid)
        else:
            row = self._connection.execute(
                """SELECT id FROM manual_analyses WHERE symbol = ? AND strategy_version = ?
                   AND candle_close_time = ? AND snapshot_hash = ?""", key
            ).fetchone()
            if row is None:
                raise sqlite3.IntegrityError("unable to resolve idempotent manual analysis")
            analysis_id = int(row[0])
        self._commit()
        return analysis_id, inserted

    @staticmethod
    def _manual_row(row: tuple[Any, ...] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = json.loads(row[1])
        payload.update({"id": row[0], "payload_hash": row[2], "recorded_at": row[3]})
        return payload

    def manual_analysis_by_id(self, analysis_id: int) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT id, payload_json, payload_hash, recorded_at FROM manual_analyses WHERE id = ?",
            (analysis_id,),
        ).fetchone()
        return self._manual_row(row)

    def latest_manual_analysis(self, symbol: str | None = None) -> dict[str, Any] | None:
        if symbol is None:
            row = self._connection.execute(
                "SELECT id, payload_json, payload_hash, recorded_at FROM manual_analyses ORDER BY id DESC LIMIT 1"
            ).fetchone()
        else:
            row = self._connection.execute(
                """SELECT id, payload_json, payload_hash, recorded_at FROM manual_analyses
                   WHERE symbol = ? ORDER BY id DESC LIMIT 1""", (symbol,)
            ).fetchone()
        return self._manual_row(row)

    def list_manual_analyses(self, symbol: str | None = None, *, limit: int = 50) -> list[dict[str, Any]]:
        if not 1 <= limit <= 200:
            raise ValueError("manual analysis history limit must be between 1 and 200")
        if symbol is None:
            rows = self._connection.execute(
                """SELECT id, payload_json, payload_hash, recorded_at FROM manual_analyses
                   ORDER BY id DESC LIMIT ?""", (limit,)
            ).fetchall()
        else:
            rows = self._connection.execute(
                """SELECT id, payload_json, payload_hash, recorded_at FROM manual_analyses
                   WHERE symbol = ? ORDER BY id DESC LIMIT ?""", (symbol, limit)
            ).fetchall()
        return [item for row in rows if (item := self._manual_row(row)) is not None]

    def ensure_paper_account(self, initial_equity: str, config_hash: str, nexus_mode: str = "OFF") -> None:
        self._connection.execute(
            """INSERT OR IGNORE INTO paper_account
                   (id, initial_equity, cash, config_hash, nexus_mode) VALUES (1, ?, ?, ?, ?)""",
            (initial_equity, initial_equity, config_hash, nexus_mode),
        )
        row = self._connection.execute(
            "SELECT config_hash, nexus_mode FROM paper_account WHERE id = 1"
        ).fetchone()
        if row is None or row[0] != config_hash:
            raise ValueError("PAPER database configuration hash mismatch; use a new database")
        modes = {"OFF": 0, "AUDIT": 1, "ENFORCE": 2}
        if nexus_mode not in modes or row[1] not in modes:
            raise ValueError("invalid persisted Nexus mode")
        if modes[nexus_mode] < modes[row[1]]:
            raise ValueError("Maxx Nexus mode downgrade requires a new PAPER database")
        if modes[nexus_mode] > modes[row[1]]:
            self._connection.execute(
                "UPDATE paper_account SET nexus_mode = ? WHERE id = 1", (nexus_mode,)
            )
        self._commit()

    def paper_account(self) -> dict[str, str]:
        row = self._connection.execute(
            "SELECT initial_equity, cash, config_hash, nexus_mode FROM paper_account WHERE id = 1"
        ).fetchone()
        if row is None:
            raise ValueError("PAPER account is not initialized")
        return {
            "initial_equity": row[0], "cash": row[1], "config_hash": row[2],
            "nexus_mode": row[3],
        }

    def open_positions(self) -> list[dict[str, Any]]:
        columns = (
            "id", "symbol", "entry_day", "entry_time_ms", "entry_candle_close_time",
            "entry_price", "quantity", "entry_fee", "hard_stop", "trailing_stop",
            "peak_price", "last_mark", "nexus_permit_id",
        )
        rows = self._connection.execute(
            """SELECT id, symbol, entry_day, entry_time_ms, entry_candle_close_time,
                       entry_price, quantity, entry_fee, hard_stop, trailing_stop, peak_price, last_mark,
                       nexus_permit_id
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

    def open_paper_position(self, position: dict[str, Any], event: dict[str, Any], new_cash: str,
                            nexus_permit_id: int | None = None) -> bool:
        payload = json.dumps(event["payload"], sort_keys=True, separators=(",", ":"))
        self._connection.execute(
            """INSERT INTO paper_events
                   (event_key, day_utc, generated_at_ms, event_type, symbol, payload_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (event["event_key"], event["day_utc"], event["generated_at_ms"], event["event_type"], event["symbol"], payload),
        )
        cursor = self._connection.execute(
            """INSERT INTO paper_positions
                   (symbol, entry_day, entry_time_ms, entry_candle_close_time,
                    entry_price, quantity, entry_fee, hard_stop, trailing_stop,
                    peak_price, last_mark, nexus_permit_id, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')""",
            (
                position["symbol"], position["entry_day"], position["entry_time_ms"],
                position["entry_candle_close_time"], position["entry_price"],
                position["quantity"], position["entry_fee"], position["hard_stop"],
                position["trailing_stop"], position["peak_price"], position["last_mark"],
                nexus_permit_id,
            ),
        )
        if nexus_permit_id is not None:
            consumed = self._connection.execute(
                """UPDATE paper_permits SET status = 'CONSUMED', consumed_position_id = ?,
                           consumed_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND status = 'ISSUED' AND symbol = ? AND candle_close_time = ?""",
                (cursor.lastrowid, nexus_permit_id, position["symbol"], position["entry_candle_close_time"]),
            )
            if consumed.rowcount != 1:
                raise sqlite3.IntegrityError("Nexus PAPER permit is invalid or already consumed")
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

    def record_probability_bar(self, symbol: str, candle_close_time: int, close: str) -> bool:
        cursor = self._connection.execute(
            """INSERT OR IGNORE INTO probability_market_bars
                   (symbol, candle_close_time, close) VALUES (?, ?, ?)""",
            (symbol, candle_close_time, close),
        )
        if cursor.rowcount == 0:
            existing = self._connection.execute(
                "SELECT close FROM probability_market_bars WHERE symbol = ? AND candle_close_time = ?",
                (symbol, candle_close_time),
            ).fetchone()
            if existing is None or existing[0] != close:
                raise ValueError("conflicting probability market bar")
        self._commit()
        return cursor.rowcount == 1

    def record_probability_candidate(
        self, *, symbol: str, candle_close_time: int, strategy_version: str,
        config_hash: str, entry_close: str, features: dict[str, float]
    ) -> bool:
        text = json.dumps(features, sort_keys=True, separators=(",", ":"), allow_nan=False)
        cursor = self._connection.execute(
            """INSERT OR IGNORE INTO probability_candidates
                   (symbol, candle_close_time, strategy_version, config_hash, entry_close, features_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (symbol, candle_close_time, strategy_version, config_hash, entry_close, text),
        )
        if cursor.rowcount == 0:
            existing = self._connection.execute(
                """SELECT entry_close, features_json FROM probability_candidates
                   WHERE symbol = ? AND candle_close_time = ? AND strategy_version = ? AND config_hash = ?""",
                (symbol, candle_close_time, strategy_version, config_hash),
            ).fetchone()
            if existing is None or existing != (entry_close, text):
                raise ValueError("conflicting probability candidate")
        self._commit()
        return cursor.rowcount == 1

    def unlabeled_probability_candidates(self) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT id, symbol, candle_close_time, entry_close
               FROM probability_candidates WHERE label IS NULL ORDER BY candle_close_time, symbol"""
        ).fetchall()
        return [dict(zip(("id", "symbol", "candle_close_time", "entry_close"), row, strict=True)) for row in rows]

    def probability_bar_close(self, symbol: str, candle_close_time: int) -> str | None:
        row = self._connection.execute(
            "SELECT close FROM probability_market_bars WHERE symbol = ? AND candle_close_time = ?",
            (symbol, candle_close_time),
        ).fetchone()
        return None if row is None else str(row[0])

    def label_probability_candidate(self, candidate_id: int, *, label: int,
                                    outcome_close_time: int, net_return: str) -> None:
        cursor = self._connection.execute(
            """UPDATE probability_candidates
               SET label = ?, outcome_close_time = ?, net_return = ?, labeled_at = CURRENT_TIMESTAMP
               WHERE id = ? AND label IS NULL""",
            (label, outcome_close_time, net_return, candidate_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("probability candidate was already labeled")
        self._commit()

    def matured_probability_candidates(self, strategy_version: str, config_hash: str) -> list[dict[str, Any]]:
        rows = self._connection.execute(
            """SELECT symbol, candle_close_time, features_json, label
               FROM probability_candidates
               WHERE strategy_version = ? AND config_hash = ? AND label IS NOT NULL
               ORDER BY candle_close_time, symbol""",
            (strategy_version, config_hash),
        ).fetchall()
        return [
            {"symbol": row[0], "candle_close_time": row[1], "features": json.loads(row[2]), "label": row[3]}
            for row in rows
        ]

    def probability_status(self) -> dict[str, Any]:
        row = self._connection.execute(
            """SELECT COUNT(*), SUM(CASE WHEN label IS NOT NULL THEN 1 ELSE 0 END),
                      SUM(CASE WHEN label = 1 THEN 1 ELSE 0 END)
               FROM probability_candidates"""
        ).fetchone()
        return {
            "collected": int(row[0] or 0),
            "matured": int(row[1] or 0),
            "positive": int(row[2] or 0),
            "horizon_daily_candles": 5,
        }

    def pending_probability_outcomes(self) -> tuple[tuple[str, int], ...]:
        rows = self._connection.execute(
            """SELECT symbol, candle_close_time + ? AS outcome_time
               FROM probability_candidates WHERE label IS NULL
               ORDER BY symbol, outcome_time""",
            (5 * DAY_MS,),
        ).fetchall()
        return tuple((str(row[0]), int(row[1])) for row in rows)

    def pending_probability_symbols(self) -> tuple[str, ...]:
        return tuple(sorted({symbol for symbol, _ in self.pending_probability_outcomes()}))

    def record_nexus_review(self, review: dict[str, Any]) -> int:
        if not self._paper_transaction:
            raise ValueError("Nexus reviews must be recorded inside a PAPER cycle")
        text = json.dumps(review, sort_keys=True, separators=(",", ":"), allow_nan=False)
        cursor = self._connection.execute(
            """INSERT OR IGNORE INTO nexus_reviews
                   (candidate_key, symbol, candle_close_time, mode, disposition, review_hash, payload_json)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                review["candidate_key"], review["symbol"], review["candle_close_time"],
                review["mode"], review["disposition"], review["review_hash"], text,
            ),
        )
        if cursor.rowcount == 1:
            return int(cursor.lastrowid)
        row = self._connection.execute(
            "SELECT id, review_hash FROM nexus_reviews WHERE candidate_key = ?",
            (review["candidate_key"],),
        ).fetchone()
        if row is None or row[1] != review["review_hash"]:
            raise sqlite3.IntegrityError("conflicting Maxx Nexus review")
        return int(row[0])

    def issue_paper_permit(self, review_id: int) -> int:
        if not self._paper_transaction:
            raise ValueError("Nexus permits must be issued inside a PAPER cycle")
        row = self._connection.execute(
            """SELECT symbol, candle_close_time, mode, disposition, review_hash
               FROM nexus_reviews WHERE id = ?""",
            (review_id,),
        ).fetchone()
        if row is None or row[2] != "ENFORCE" or row[3] != "PERMIT_ELIGIBLE":
            raise ValueError("Nexus review is not eligible for a PAPER permit")
        permit = {
            "schema_version": 1,
            "operation": "OPEN_PAPER_POSITION",
            "execution_mode": "PAPER",
            "symbol": row[0],
            "candle_close_time": row[1],
            "review_hash": row[4],
            "orders_enabled": False,
        }
        permit["permit_hash"] = _payload_hash(permit, "permit_hash")
        text = json.dumps(permit, sort_keys=True, separators=(",", ":"), allow_nan=False)
        cursor = self._connection.execute(
            """INSERT INTO paper_permits
                   (review_id, symbol, candle_close_time, status, payload_json)
               VALUES (?, ?, ?, 'ISSUED', ?)""",
            (review_id, row[0], row[1], text),
        )
        return int(cursor.lastrowid)

    def record_post_trade_audit(self, position_id: int, payload: dict[str, Any]) -> bool:
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        cursor = self._connection.execute(
            """INSERT OR IGNORE INTO post_trade_audits (position_id, status, payload_json)
               VALUES (?, ?, ?)""",
            (position_id, payload["status"], text),
        )
        self._commit()
        return cursor.rowcount == 1

    def _nexus_permit_integrity(
        self, *, permit_id: int, status: str, position_id: int | None,
        payload_text: str, review_id: int, expected_review_id: int,
        review: dict[str, Any],
        relational_symbol: str, relational_candle: int,
    ) -> bool:
        try:
            permit = json.loads(payload_text)
        except json.JSONDecodeError:
            return False
        expected_keys = {
            "schema_version", "operation", "execution_mode", "symbol",
            "candle_close_time", "review_hash", "orders_enabled", "permit_hash",
        }
        if (
            not isinstance(permit, dict)
            or set(permit) != expected_keys
            or permit.get("permit_hash") != _payload_hash(permit, "permit_hash")
            or permit.get("schema_version") != 1
            or permit.get("operation") != "OPEN_PAPER_POSITION"
            or permit.get("execution_mode") != "PAPER"
            or permit.get("orders_enabled") is not False
            or permit.get("symbol") != relational_symbol
            or permit.get("candle_close_time") != relational_candle
            or permit.get("review_hash") != review.get("review_hash")
            or review_id != expected_review_id
            or relational_symbol != review.get("symbol")
            or relational_candle != review.get("candle_close_time")
            or review.get("mode") != "ENFORCE"
            or review.get("disposition") != "PERMIT_ELIGIBLE"
        ):
            return False
        if status == "ISSUED":
            linked = self._connection.execute(
                "SELECT 1 FROM paper_positions WHERE nexus_permit_id = ? LIMIT 1",
                (permit_id,),
            ).fetchone()
            return position_id is None and linked is None
        if status != "CONSUMED" or position_id is None:
            return False
        position = self._connection.execute(
            """SELECT symbol, entry_candle_close_time, nexus_permit_id
               FROM paper_positions WHERE id = ?""",
            (position_id,),
        ).fetchone()
        return position == (relational_symbol, relational_candle, permit_id)

    def nexus_status(self, symbol: str | None = None, *, limit: int = 20) -> dict[str, Any]:
        if not 1 <= limit <= 200:
            raise ValueError("Nexus status limit must be between 1 and 200")
        query = """SELECT r.id, r.payload_json, r.recorded_at, p.id, p.status,
                          p.consumed_position_id, r.symbol, r.candle_close_time,
                          r.mode, r.disposition, r.review_hash,
                          p.payload_json, p.review_id, p.symbol, p.candle_close_time
                   FROM nexus_reviews r LEFT JOIN paper_permits p ON p.review_id = r.id"""
        params: tuple[Any, ...]
        if symbol is None:
            query += " ORDER BY r.id DESC LIMIT ?"
            params = (limit,)
        else:
            query += " WHERE r.symbol = ? ORDER BY r.id DESC LIMIT ?"
            params = (symbol, limit)
        rows = self._connection.execute(query, params).fetchall()
        reviews = []
        for row in rows:
            try:
                payload = json.loads(row[1])
            except json.JSONDecodeError:
                payload = {}
            indexed_values_match = (
                payload.get("symbol") == row[6]
                and payload.get("candle_close_time") == row[7]
                and payload.get("mode") == row[8]
                and payload.get("disposition") == row[9]
                and payload.get("review_hash") == row[10]
            )
            permit_integrity = (
                row[3] is None
                or self._nexus_permit_integrity(
                    permit_id=int(row[3]), status=row[4], position_id=row[5],
                    payload_text=row[11], review_id=int(row[12]), expected_review_id=int(row[0]),
                    review=payload,
                    relational_symbol=row[13], relational_candle=int(row[14]),
                )
            )
            if not indexed_values_match or not validate_nexus_review(payload) or not permit_integrity:
                reviews.append({
                    "id": row[0], "recorded_at": row[2], "status": "INTEGRITY_ERROR",
                    "permit": None, "orders_enabled": False,
                })
                continue
            payload.update({
                "id": row[0], "recorded_at": row[2], "status": "INTEGRITY_OK",
                "permit": None if row[3] is None else {
                    "id": row[3], "status": row[4], "position_id": row[5],
                },
            })
            reviews.append(payload)
        count_query = """SELECT id, payload_json, symbol, candle_close_time, mode,
                                      disposition, review_hash FROM nexus_reviews"""
        count_params: tuple[Any, ...] = ()
        if symbol is not None:
            count_query += " WHERE symbol = ?"
            count_params = (symbol,)
        count_rows = self._connection.execute(count_query, count_params).fetchall()
        valid_dispositions: list[str] = []
        valid_review_ids: set[int] = set()
        integrity_errors = 0
        for count_row in count_rows:
            try:
                count_payload = json.loads(count_row[1])
            except json.JSONDecodeError:
                count_payload = {}
            valid = (
                count_payload.get("symbol") == count_row[2]
                and count_payload.get("candle_close_time") == count_row[3]
                and count_payload.get("mode") == count_row[4]
                and count_payload.get("disposition") == count_row[5]
                and count_payload.get("review_hash") == count_row[6]
                and validate_nexus_review(count_payload)
            )
            if valid:
                valid_review_ids.add(int(count_row[0]))
                valid_dispositions.append(str(count_row[5]))
            else:
                integrity_errors += 1
        permit_count_query = """SELECT p.id, p.status, p.consumed_position_id,
                                             p.payload_json, p.review_id, r.id,
                                             r.payload_json, p.symbol, p.candle_close_time
                                      FROM paper_permits p
                                      JOIN nexus_reviews r ON r.id = p.review_id"""
        permit_count_params: tuple[Any, ...] = ()
        if symbol is not None:
            permit_count_query += " WHERE r.symbol = ?"
            permit_count_params = (symbol,)
        permit_integrity_errors = 0
        permit_count = 0
        permit_integrity_ok_count = 0
        full_chain_integrity_ok_count = 0
        for permit_row in self._connection.execute(permit_count_query, permit_count_params).fetchall():
            permit_count += 1
            try:
                permit_review = json.loads(permit_row[6])
            except json.JSONDecodeError:
                permit_review = {}
            permit_valid = (
                int(permit_row[5]) in valid_review_ids
                and validate_nexus_review(permit_review)
                and self._nexus_permit_integrity(
                    permit_id=int(permit_row[0]), status=permit_row[1], position_id=permit_row[2],
                    payload_text=permit_row[3], review_id=int(permit_row[4]),
                    expected_review_id=int(permit_row[5]), review=permit_review,
                    relational_symbol=permit_row[7], relational_candle=int(permit_row[8]),
                )
            )
            if not permit_valid:
                permit_integrity_errors += 1
            else:
                permit_integrity_ok_count += 1
                if permit_row[1] == "CONSUMED" and permit_row[2] is not None:
                    full_chain_integrity_ok_count += 1
        return {
            "system": "MAXX_NEXUS",
            "review_count": len(count_rows),
            "review_integrity_ok_count": len(valid_review_ids),
            "review_integrity_error_count": integrity_errors,
            "permit_count": permit_count,
            "permit_integrity_ok_count": permit_integrity_ok_count,
            "permit_integrity_error_count": permit_integrity_errors,
            "full_chain_integrity_ok_count": full_chain_integrity_ok_count,
            "blocked_or_shadow_blocked": sum(
                item in {"BLOCKED", "SHADOW_BLOCK"} for item in valid_dispositions
            ),
            "reviews": reviews,
            "orders_enabled": False,
        }

    def verify_consumed_nexus_permit(self, position_id: int, permit_id: int) -> bool:
        row = self._connection.execute(
            """SELECT p.status, p.consumed_position_id, p.symbol, p.candle_close_time,
                      r.mode, r.disposition, r.payload_json, r.review_hash,
                      x.symbol, x.entry_candle_close_time, p.payload_json,
                      p.review_id, r.id
               FROM paper_permits p
               JOIN nexus_reviews r ON r.id = p.review_id
               JOIN paper_positions x ON x.id = ?
               WHERE p.id = ?""",
            (position_id, permit_id),
        ).fetchone()
        if row is None:
            return False
        try:
            review = json.loads(row[6])
        except json.JSONDecodeError:
            return False
        permit_integrity = self._nexus_permit_integrity(
            permit_id=permit_id, status=row[0], position_id=row[1],
            payload_text=row[10], review_id=int(row[11]), expected_review_id=int(row[12]),
            review=review, relational_symbol=row[2], relational_candle=int(row[3]),
        )
        return bool(
            permit_integrity
            and row[0] == "CONSUMED"
            and row[1] == position_id
            and row[2] == row[8]
            and row[3] == row[9]
            and row[4] == "ENFORCE"
            and row[5] == "PERMIT_ELIGIBLE"
            and review.get("review_hash") == row[7]
            and validate_nexus_review(review)
        )

    def assert_nexus_mode_not_downgraded(self, nexus_mode: str) -> None:
        row = self._connection.execute(
            "SELECT nexus_mode FROM paper_account WHERE id = 1"
        ).fetchone()
        if row is None:
            return
        modes = {"OFF": 0, "AUDIT": 1, "ENFORCE": 2}
        if nexus_mode not in modes or row[0] not in modes:
            raise ValueError("invalid persisted Nexus mode")
        if modes[nexus_mode] < modes[row[0]]:
            raise ValueError("Maxx Nexus mode downgrade requires a new PAPER database")
        if modes[nexus_mode] > modes[row[0]]:
            self._connection.execute(
                "UPDATE paper_account SET nexus_mode = ? WHERE id = 1", (nexus_mode,)
            )
            self._connection.commit()
