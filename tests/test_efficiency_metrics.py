from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval.evolution import summarize_metrics
from agent_eval.model import MetricObjective


class EfficiencyMetricsTest(unittest.TestCase):
    def test_trace_efficiency_metrics_are_available_to_objectives(self) -> None:
        summary = {
            "results": [
                {
                    "hard_pass": True,
                    "soft_warning_count": 0,
                    "trace": {
                        "fields": {
                            "llm_call_count": 2,
                            "input_tokens": 100,
                            "output_tokens": 20,
                            "total_tokens": 120,
                            "cost_usd": 0.01,
                        },
                        "events": [{"duration_ms": 5}],
                    },
                }
            ],
            "hard_failures": 0,
            "soft_warnings": 0,
        }
        objectives = (
            MetricObjective("calls", "llm_calls", direction="minimize"),
            MetricObjective("tokens", "total_tokens", direction="minimize"),
        )

        metrics = summarize_metrics(summary, objectives)

        self.assertEqual(metrics["efficiency"]["mean_llm_calls"], 2.0)
        self.assertEqual(metrics["efficiency"]["mean_total_tokens"], 120.0)
        self.assertEqual(metrics["efficiency"]["mean_cost_usd"], 0.01)
        self.assertEqual(metrics["objectives"]["calls"], 2.0)
        self.assertEqual(metrics["objectives"]["tokens"], 120.0)

    def test_model_call_count_can_be_recovered_from_events(self) -> None:
        summary = {
            "results": [
                {
                    "hard_pass": True,
                    "soft_warning_count": 0,
                    "trace": {
                        "events": [
                            {"fields": {"span_type": "generation"}},
                            {"fields": {"span_type": "function"}},
                            {"fields": {"span_type": "response"}},
                        ]
                    },
                }
            ],
            "hard_failures": 0,
            "soft_warnings": 0,
        }

        metrics = summarize_metrics(summary, ())

        self.assertEqual(metrics["efficiency"]["mean_llm_calls"], 2.0)
        self.assertIsNone(metrics["efficiency"]["mean_total_tokens"])


if __name__ == "__main__":
    unittest.main()
