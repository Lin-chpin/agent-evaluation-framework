from __future__ import annotations

import unittest

from agent_eval.review_samples import promote_review_record


class ReviewSampleTests(unittest.TestCase):
    def test_unresolved_review_stays_out_of_gate_datasets(self) -> None:
        record = {"id": "ambiguous", "input": {}, "metadata": {}}

        promoted = promote_review_record(
            record,
            outcome="UNRESOLVED",
            conclusion="business owner cannot decide yet",
            role="pending",
            reviewer="owner-a",
            reviewed_at="2026-08-31T00:00:00+00:00",
        )

        self.assertEqual(promoted["expected"], {})
        self.assertEqual(promoted["metadata"]["review_status"], "unresolved")

    def test_adjudication_preserves_the_previous_gold(self) -> None:
        record = {
            "id": "review-7",
            "input": {},
            "metadata": {
                "human_review_outcome": "CORRECT",
                "gold_correction": None,
                "review_source": "owner-a",
                "reviewed_at": "first",
            },
        }

        promoted = promote_review_record(
            record,
            outcome="PARTIALLY_CORRECT",
            conclusion="unsupported capability claim",
            role="holdout",
            reviewer="owner-a",
            reviewed_at="second",
        )

        self.assertEqual(promoted["expected"], {"verdict": "INCORRECT"})
        self.assertEqual(
            [item["outcome"] for item in promoted["metadata"]["review_history"]],
            ["CORRECT", "PARTIALLY_CORRECT"],
        )

    def test_improvement_rejects_a_correct_report(self) -> None:
        with self.assertRaisesRegex(ValueError, "improvement requires"):
            promote_review_record(
                {"id": "correct", "input": {}},
                outcome="CORRECT",
                conclusion="correct",
                role="improvement",
                reviewer="owner-a",
            )


if __name__ == "__main__":
    unittest.main()
