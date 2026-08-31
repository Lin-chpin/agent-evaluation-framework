from __future__ import annotations

from typing import Any, Mapping, Sequence


def classification_metrics(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    agreement = false_positives = false_negatives = 0
    field_correct: dict[str, int] = {}
    field_total: dict[str, int] = {}
    evaluated = 0
    predictions: dict[str, str] = {}

    for result in results:
        case = result.get("case", {})
        expected = case.get("expected", {})
        fields = (result.get("trace") or {}).get("fields", {})
        expected_verdict = expected.get("verdict")
        actual_verdict = fields.get("verdict")
        if expected_verdict not in {"CORRECT", "INCORRECT"} or actual_verdict not in {
            "CORRECT",
            "INCORRECT",
        }:
            continue
        evaluated += 1
        predictions[str(case.get("case_id", ""))] = actual_verdict
        agreement += actual_verdict == expected_verdict
        false_positives += actual_verdict == "INCORRECT" and expected_verdict == "CORRECT"
        false_negatives += actual_verdict == "CORRECT" and expected_verdict == "INCORRECT"
        for field, expected_value in expected.items():
            if field not in fields:
                continue
            field_total[field] = field_total.get(field, 0) + 1
            field_correct[field] = field_correct.get(field, 0) + (fields[field] == expected_value)

    return {
        "case_count": len(results),
        "evaluated_count": evaluated,
        "agreement_count": agreement,
        "agreement_rate": agreement / evaluated if evaluated else None,
        "false_positive_count": false_positives,
        "false_negative_count": false_negatives,
        "field_accuracy": {
            field: field_correct.get(field, 0) / total
            for field, total in sorted(field_total.items())
        },
        "predictions": predictions,
    }


def repeat_stability(runs: Sequence[Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    if len(runs) < 2:
        return {"run_count": len(runs), "comparable_case_count": 0, "stability_rate": None}
    by_run = [classification_metrics(results)["predictions"] for results in runs]
    common = set(by_run[0]).intersection(*(set(run) for run in by_run[1:]))
    stable = sum(len({run[case_id] for run in by_run}) == 1 for case_id in common)
    return {
        "run_count": len(runs),
        "comparable_case_count": len(common),
        "stable_case_count": stable,
        "stability_rate": stable / len(common) if common else None,
    }

