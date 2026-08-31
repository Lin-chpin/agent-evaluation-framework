from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


OUTCOMES = {"CORRECT", "PARTIALLY_CORRECT", "INCORRECT", "UNRESOLVED"}
ROLES = {"improvement", "regression", "holdout", "pending"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def promote_review_record(
    record: Mapping[str, Any],
    *,
    outcome: str,
    conclusion: str,
    role: str,
    reviewer: str,
    reviewed_at: str | None = None,
) -> dict[str, Any]:
    outcome = outcome.upper()
    if outcome not in OUTCOMES:
        raise ValueError(f"unsupported review outcome: {outcome}")
    if role not in ROLES:
        raise ValueError(f"unsupported dataset role: {role}")
    if (outcome == "UNRESOLVED") != (role == "pending"):
        raise ValueError("UNRESOLVED reviews must stay pending, and pending requires UNRESOLVED")
    if role == "regression" and outcome != "CORRECT":
        raise ValueError("regression requires a CORRECT human outcome")
    if role == "improvement" and outcome not in {"PARTIALLY_CORRECT", "INCORRECT"}:
        raise ValueError("improvement requires a PARTIALLY_CORRECT or INCORRECT outcome")
    if not conclusion.strip() or not reviewer.strip():
        raise ValueError("review conclusion and reviewer must not be empty")

    promoted = dict(record)
    case_id = str(promoted.get("id") or promoted.get("case_id") or "").strip()
    if not case_id:
        raise ValueError("review sample requires a non-empty id")
    metadata = dict(promoted.get("metadata", {}))
    history = list(metadata.get("review_history", []))
    previous = metadata.get("human_review_outcome")
    if previous and not history:
        history.append(
            {
                "outcome": previous,
                "conclusion": metadata.get("gold_correction"),
                "reviewer": metadata.get("review_source", "unknown"),
                "reviewed_at": metadata.get("reviewed_at"),
            }
        )

    timestamp = reviewed_at or _now()
    history.append(
        {
            "outcome": outcome,
            "conclusion": conclusion,
            "reviewer": reviewer,
            "reviewed_at": timestamp,
        }
    )
    gate_verdict = None if outcome == "UNRESOLVED" else (
        "CORRECT" if outcome == "CORRECT" else "INCORRECT"
    )
    metadata.update(
        {
            "dataset_role": role,
            "human_reviewed": True,
            "review_status": "unresolved" if outcome == "UNRESOLVED" else "confirmed",
            "human_review_outcome": outcome,
            "gate_verdict": gate_verdict,
            "gold_correction": None if outcome == "CORRECT" else conclusion,
            "reviewed_at": timestamp,
            "review_source": reviewer,
            "review_history": history,
        }
    )
    promoted["metadata"] = metadata
    promoted["expected"] = {} if gate_verdict is None else {"verdict": gate_verdict}
    return promoted


def write_review_record(path: Path, record: Mapping[str, Any]) -> None:
    rows: list[dict[str, Any]] = []
    if path.exists():
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    record_id = str(record.get("id") or record.get("case_id"))
    replaced = False
    for index, row in enumerate(rows):
        if str(row.get("id") or row.get("case_id")) == record_id:
            rows[index] = dict(record)
            replaced = True
            break
    if not replaced:
        rows.append(dict(record))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    temporary.replace(path)

