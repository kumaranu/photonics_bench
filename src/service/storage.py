import json
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _get_git_revision() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL)
            .decode("utf-8")
            .strip()
        )
    except Exception:
        return "unknown"


@dataclass
class RunRecord:
    run_id: str
    created_at: str
    completed_at: str | None
    operator: str
    notes: str
    start_nm: float
    stop_nm: float
    step_nm: float
    retries_per_point: int
    status: str
    provenance: dict[str, Any]


class RunStorage:
    """SQLite persistence storing full experimental provenance alongside sweep points."""

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    operator TEXT NOT NULL,
                    notes TEXT NOT NULL,
                    start_nm REAL NOT NULL,
                    stop_nm REAL NOT NULL,
                    step_nm REAL NOT NULL,
                    retries_per_point INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    provenance_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                    step_index INTEGER NOT NULL,
                    target_nm REAL NOT NULL,
                    actual_nm REAL,
                    power_dbm REAL,
                    attempts INTEGER NOT NULL,
                    error_msg TEXT,
                    recorded_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_readings_run_id ON readings(run_id);
                """
            )

    def create_run(
        self,
        run_id: str,
        operator: str,
        notes: str,
        start_nm: float,
        stop_nm: float,
        step_nm: float,
        retries_per_point: int,
        instruments: dict[str, Any],
        fault_config: dict[str, Any],
    ) -> RunRecord:
        now = datetime.now(timezone.utc).isoformat()
        provenance = {
            "code_revision": _get_git_revision(),
            "instruments": instruments,
            "fault_config": fault_config,
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, created_at, operator, notes, start_nm, stop_nm,
                    step_nm, retries_per_point, status, provenance_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    now,
                    operator,
                    notes,
                    start_nm,
                    stop_nm,
                    step_nm,
                    retries_per_point,
                    "running",
                    json.dumps(provenance),
                ),
            )
        return RunRecord(
            run_id=run_id,
            created_at=now,
            completed_at=None,
            operator=operator,
            notes=notes,
            start_nm=start_nm,
            stop_nm=stop_nm,
            step_nm=step_nm,
            retries_per_point=retries_per_point,
            status="running",
            provenance=provenance,
        )

    def append_reading(
        self,
        run_id: str,
        step_index: int,
        target_nm: float,
        actual_nm: float | None,
        power_dbm: float | None,
        attempts: int,
        error_msg: str | None,
    ) -> dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO readings (
                    run_id, step_index, target_nm, actual_nm, power_dbm,
                    attempts, error_msg, recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, step_index, target_nm, actual_nm, power_dbm, attempts, error_msg, now),
            )
        return {
            "step_index": step_index,
            "target_nm": target_nm,
            "actual_nm": actual_nm,
            "power_dbm": power_dbm,
            "attempts": attempts,
            "error_msg": error_msg,
            "recorded_at": now,
        }

    def finish_run(self, run_id: str, status: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, completed_at = ? WHERE run_id = ?",
                (status, now, run_id),
            )

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.*, COUNT(rd.id) AS point_count,
                       SUM(CASE WHEN rd.power_dbm IS NOT NULL THEN 1 ELSE 0 END) AS valid_points
                FROM runs r
                LEFT JOIN readings rd ON r.run_id = rd.run_id
                GROUP BY r.run_id
                ORDER BY r.created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        results = []
        for row in rows:
            item = dict(row)
            item["provenance"] = json.loads(item.pop("provenance_json"))
            results.append(item)
        return results

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not run_row:
                return None
            readings_rows = conn.execute(
                "SELECT * FROM readings WHERE run_id = ? ORDER BY step_index ASC", (run_id,)
            ).fetchall()

        data = dict(run_row)
        data["provenance"] = json.loads(data.pop("provenance_json"))
        data["readings"] = [dict(r) for r in readings_rows]
        return data
