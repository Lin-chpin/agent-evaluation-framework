from __future__ import annotations

import unittest

from examples.evaluator_skill_human_ai_evolution_adapter import _improvement_evidence


class EvaluatorSkillAIEvidenceTests(unittest.TestCase):
    def test_candidate_evidence_contains_failed_improvement_only(self) -> None:
        summary = {
            "results": [
                {
                    "hard_pass": False,
                    "soft_warning_count": 0,
                    "case": {
                        "case_id": "bad-1",
                        "payload": {
                            "facts": "fact",
                            "agent_output": "output",
                            "evaluation_report": "report",
                        },
                        "metadata": {
                            "dataset_role": "improvement",
                            "human_review_outcome": "PARTIALLY_CORRECT",
                            "gate_verdict": "INCORRECT",
                            "gold_correction": "correction",
                        },
                    },
                },
                {
                    "hard_pass": True,
                    "soft_warning_count": 0,
                    "case": {
                        "case_id": "already-correct",
                        "payload": {},
                        "metadata": {"dataset_role": "improvement"},
                    },
                },
            ]
        }

        evidence = _improvement_evidence(summary)

        self.assertEqual([item["case_id"] for item in evidence], ["bad-1"])
        self.assertEqual(evidence[0]["gold_correction"], "correction")

    def test_candidate_evidence_rejects_non_improvement_role(self) -> None:
        summary = {
            "results": [
                {
                    "hard_pass": False,
                    "soft_warning_count": 0,
                    "case": {
                        "case_id": "leaked-holdout",
                        "payload": {},
                        "metadata": {"dataset_role": "holdout"},
                    },
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "improvement cases only"):
            _improvement_evidence(summary)


if __name__ == "__main__":
    unittest.main()
