from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Mapping

from .locking import exclusive_file_lock
from .model import CaseResult, to_jsonable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResultStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = Lock()
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    @contextmanager
    def lock_run(self, run_id: str) -> Iterator[Path]:
        safe_run_id = run_id.strip()
        if not safe_run_id or Path(safe_run_id).name != safe_run_id:
            raise ValueError("run_id must be a safe non-empty name")
        lock_path = self.path.parent / f".{self.path.name}.locks" / f"{safe_run_id}.lock"
        with exclusive_file_lock(lock_path, f"evaluation run {run_id}"):
            yield lock_path

    def _create_schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                adapter TEXT NOT NULL,
                suite TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                metadata_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS case_results (
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                trace_id TEXT,
                hard_pass INTEGER NOT NULL,
                soft_warning_count INTEGER NOT NULL,
                result_json TEXT NOT NULL,
                PRIMARY KEY (run_id, case_id)
            );
            CREATE TABLE IF NOT EXISTS run_cases (
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                case_sha256 TEXT NOT NULL,
                PRIMARY KEY (run_id, case_id)
            );
            CREATE TABLE IF NOT EXISTS reviews (
                run_id TEXT NOT NULL,
                case_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                final_conclusion TEXT NOT NULL,
                reviewed_at TEXT NOT NULL,
                PRIMARY KEY (run_id, case_id)
            );
            CREATE TABLE IF NOT EXISTS evolution_experiments (
                experiment_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                decision TEXT NOT NULL,
                result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def start_run(
        self,
        run_id: str,
        adapter: str,
        suite: str,
        source: str,
        metadata: Mapping[str, Any],
        resume: bool = False,
        case_manifest: Mapping[str, str] | None = None,
    ) -> None:
        with self.lock:
            existing = self.connection.execute(
                "SELECT adapter, suite, source, metadata_json FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if existing:
                if not resume:
                    raise ValueError(f"run already exists; use resume to continue: {run_id}")
                previous_metadata = json.loads(existing["metadata_json"])
                stable_previous = {
                    key: value for key, value in previous_metadata.items() if key != "case_count"
                }
                stable_current = {
                    key: value for key, value in metadata.items() if key != "case_count"
                }
                if (
                    existing["adapter"] != adapter
                    or existing["suite"] != suite
                    or existing["source"] != source
                    or stable_previous != stable_current
                ):
                    raise ValueError("resume run identity or execution configuration changed")
                if case_manifest is not None:
                    stored = {
                        row["case_id"]: row["case_sha256"]
                        for row in self.connection.execute(
                            "SELECT case_id, case_sha256 FROM run_cases WHERE run_id = ?",
                            (run_id,),
                        ).fetchall()
                    }
                    saved_count = self.connection.execute(
                        "SELECT COUNT(*) FROM case_results WHERE run_id = ?", (run_id,)
                    ).fetchone()[0]
                    if not stored and saved_count:
                        raise ValueError("cannot safely resume a legacy run without case identities")
                    missing = sorted(set(stored).difference(case_manifest))
                    changed = sorted(
                        case_id
                        for case_id, digest in stored.items()
                        if case_id in case_manifest and case_manifest[case_id] != digest
                    )
                    if missing or changed:
                        details = []
                        if missing:
                            details.append(f"missing case_id: {', '.join(missing)}")
                        if changed:
                            details.append(f"changed case_id: {', '.join(changed)}")
                        raise ValueError("resume dataset changed; " + "; ".join(details))
                    self.connection.executemany(
                        "INSERT OR IGNORE INTO run_cases (run_id, case_id, case_sha256) VALUES (?, ?, ?)",
                        ((run_id, case_id, digest) for case_id, digest in case_manifest.items()),
                    )
                self.connection.execute(
                    "UPDATE runs SET status = 'running', finished_at = NULL, metadata_json = ? WHERE run_id = ?",
                    (json.dumps(metadata, ensure_ascii=False), run_id),
                )
                self.connection.commit()
                return
            self.connection.execute(
                """
                INSERT INTO runs
                    (run_id, adapter, suite, source, status, started_at, metadata_json)
                VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (run_id, adapter, suite, source, _now(), json.dumps(metadata, ensure_ascii=False)),
            )
            if case_manifest is not None:
                self.connection.executemany(
                    "INSERT INTO run_cases (run_id, case_id, case_sha256) VALUES (?, ?, ?)",
                    ((run_id, case_id, digest) for case_id, digest in case_manifest.items()),
                )
            self.connection.commit()

    def finish_run(self, run_id: str, status: str) -> None:
        with self.lock:
            self.connection.execute(
                "UPDATE runs SET status = ?, finished_at = ? WHERE run_id = ?",
                (status, _now(), run_id),
            )
            self.connection.commit()

    def has_case(self, run_id: str, case_id: str) -> bool:
        with self.lock:
            row = self.connection.execute(
                "SELECT 1 FROM case_results WHERE run_id = ? AND case_id = ?",
                (run_id, case_id),
            ).fetchone()
        return row is not None

    def save_case(self, result: CaseResult) -> None:
        payload = to_jsonable(result)
        payload["hard_pass"] = result.hard_pass
        payload["soft_warning_count"] = result.soft_warning_count
        with self.lock:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO case_results
                    (run_id, case_id, trace_id, hard_pass, soft_warning_count, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    result.run_id,
                    result.case.case_id,
                    result.trace.trace_id if result.trace else None,
                    int(result.hard_pass),
                    result.soft_warning_count,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )
            self.connection.commit()

    def list_results(self, run_id: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                "SELECT result_json FROM case_results WHERE run_id = ? ORDER BY case_id",
                (run_id,),
            ).fetchall()
        return [json.loads(row["result_json"]) for row in rows]

    def save_review(
        self,
        run_id: str,
        case_id: str,
        decision: str,
        final_conclusion: str,
    ) -> None:
        with self.lock:
            exists = self.connection.execute(
                "SELECT 1 FROM case_results WHERE run_id = ? AND case_id = ?",
                (run_id, case_id),
            ).fetchone()
            if exists is None:
                raise ValueError(f"cannot review missing result: {run_id}/{case_id}")
            self.connection.execute(
                """
                INSERT OR REPLACE INTO reviews
                    (run_id, case_id, decision, final_conclusion, reviewed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, case_id, decision, final_conclusion, _now()),
            )
            self.connection.commit()

    def list_reviewed_results(self, decision: str) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.connection.execute(
                """
                SELECT result_json, final_conclusion
                FROM case_results
                JOIN reviews USING (run_id, case_id)
                WHERE decision = ?
                ORDER BY reviews.reviewed_at, case_results.case_id
                """,
                (decision,),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            result = json.loads(row["result_json"])
            result["human_final_conclusion"] = row["final_conclusion"]
            results.append(result)
        return results

    def save_evolution(
        self,
        experiment_id: str,
        candidate_id: str,
        decision: str,
        result: Mapping[str, Any],
    ) -> None:
        with self.lock:
            self.connection.execute(
                """
                INSERT OR REPLACE INTO evolution_experiments
                    (experiment_id, candidate_id, decision, result_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    candidate_id,
                    decision,
                    json.dumps(result, ensure_ascii=False),
                    _now(),
                ),
            )
            self.connection.commit()

    def get_evolution(self, experiment_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.connection.execute(
                "SELECT result_json FROM evolution_experiments WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
        return json.loads(row["result_json"]) if row else None

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ResultStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
