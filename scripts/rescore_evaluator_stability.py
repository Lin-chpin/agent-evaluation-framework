from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_eval.engine import load_cases
from agent_eval.evaluator_metrics import classification_metrics, repeat_stability


SOURCE = ROOT / "evidence/evaluator-skill-stability-results.json"
OUTPUT = ROOT / "evidence/evaluator-skill-stability-adjudicated.json"
DATASETS = (
    ("improvement", ROOT / "examples/evaluator_skill_human.improvement.jsonl"),
    ("regression", ROOT / "examples/evaluator_skill_human.regression.jsonl"),
    ("holdout", ROOT / "examples/evaluator_skill_human.holdout.jsonl"),
)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def main() -> int:
    source = json.loads(SOURCE.read_text("utf-8"))
    gold = {
        case.case_id: case.expected["verdict"]
        for role, path in DATASETS
        for case in load_cases(path, role)
    }
    runs = []
    for repetition in range(1, 6):
        runs.append(
            [
                {
                    "case": {
                        "case_id": call["case_id"],
                        "expected": {"verdict": gold[call["case_id"]]},
                    },
                    "trace": {"fields": {"verdict": call["verdict"]}},
                }
                for call in source["calls"]
                if call["repetition"] == repetition and call["status"] == "success"
            ]
        )
    result = {
        "schema_version": 1,
        "kind": "post-experiment gold adjudication rescore",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_result": str(SOURCE.relative_to(ROOT)),
        "source_result_sha256": sha256(SOURCE),
        "new_model_calls": 0,
        "adjudications": [
            {
                "case_id": "REVIEW-008",
                "previous_outcome": "CORRECT",
                "current_outcome": "PARTIALLY_CORRECT",
                "current_gate_verdict": "INCORRECT",
                "reason": "The report changed the explicit fact 'no data loss occurred' into the ambiguous claim 'no data loss was recorded'.",
            }
        ],
        "current_datasets": {
            role: {"path": str(path.relative_to(ROOT)), "sha256": sha256(path)}
            for role, path in DATASETS
        },
        "per_repetition_metrics": [classification_metrics(run) for run in runs],
        "repeat_stability": repeat_stability(runs),
        "limitations": [
            "The model outputs are unchanged; only the human gold was adjudicated after the experiment.",
            "The cases are human-reviewed synthetic cases, not real-business data.",
            "The adjudicated 100% agreement is descriptive for these ten cases and must not be generalized."
        ],
    }
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(
        json.dumps(
            {
                "agreement_rates": [
                    metrics["agreement_rate"] for metrics in result["per_repetition_metrics"]
                ],
                "stability_rate": result["repeat_stability"]["stability_rate"],
                "new_model_calls": 0,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
