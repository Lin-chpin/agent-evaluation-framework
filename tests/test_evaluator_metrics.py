from __future__ import annotations

import unittest

from agent_eval.evaluator_metrics import classification_metrics, repeat_stability


def result(case_id: str, expected: str, actual: str) -> dict:
    return {
        "case": {"case_id": case_id, "expected": {"verdict": expected}},
        "trace": {"fields": {"verdict": actual}},
    }


class EvaluatorMetricsTests(unittest.TestCase):
    def test_reports_agreement_false_positives_and_false_negatives(self) -> None:
        metrics = classification_metrics(
            [
                result("a", "CORRECT", "CORRECT"),
                result("b", "CORRECT", "INCORRECT"),
                result("c", "INCORRECT", "CORRECT"),
            ]
        )

        self.assertEqual(metrics["agreement_rate"], 1 / 3)
        self.assertEqual(metrics["false_positive_count"], 1)
        self.assertEqual(metrics["false_negative_count"], 1)
        self.assertEqual(metrics["field_accuracy"]["verdict"], 1 / 3)

    def test_repeat_stability_compares_the_same_cases(self) -> None:
        stability = repeat_stability(
            [
                [result("a", "CORRECT", "CORRECT"), result("b", "CORRECT", "CORRECT")],
                [result("a", "CORRECT", "CORRECT"), result("b", "CORRECT", "INCORRECT")],
            ]
        )

        self.assertEqual(stability["comparable_case_count"], 2)
        self.assertEqual(stability["stability_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
