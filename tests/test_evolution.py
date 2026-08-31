from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import (
    EvalCase,
    EvolutionCandidate,
    EvolutionPolicy,
    MetricObjective,
    NormalizedTrace,
    ProjectAdapter,
    Rule,
    RunContext,
    ScenarioGate,
    TraceEvent,
)
from agent_eval.engine import EvaluationEngine
from agent_eval.evolution import EvolutionEngine
from agent_eval.reporting import write_evolution_artifacts
from agent_eval.store import ResultStore


def adapter(version: str, candidate: bool, break_holdout: bool = False) -> ProjectAdapter:
    def call_agent(case: EvalCase, context: RunContext) -> dict:
        route_key = "candidate_route" if candidate else "baseline_route"
        route = case.metadata[route_key]
        if (
            candidate
            and break_holdout
            and case.suite == "holdout"
            and case.metadata.get("break_candidate", True)
        ):
            route = "BROKEN"
        quality_key = "candidate_quality" if candidate else "baseline_quality"
        return {
            "trace_id": f"{context.run_id}-{case.case_id}",
            "route": route,
            "quality": case.metadata[quality_key],
        }

    def read_trace(handle: dict, _: EvalCase) -> NormalizedTrace:
        return NormalizedTrace(
            trace_id=handle["trace_id"],
            final_output={"route": handle["route"]},
            events=(TraceEvent("Router", "route", duration_ms=2),),
            fields={"route": handle["route"], "quality": handle["quality"]},
            target_type="skill",
            target_id="router",
            target_version=version,
        )

    return ProjectAdapter(
        f"router-{version}",
        call_agent,
        read_trace,
        hard_gates=(Rule("route", "fields.route", expected="route"),),
        soft_quality=(),
    )


def evolution_cases() -> dict[str, list[EvalCase]]:
    return {
        "improvement": [
            EvalCase(
                "IMPROVE",
                {"message": "support request"},
                {"route": "SUPPORT"},
                "routing",
                {
                    "baseline_route": "OTHER",
                    "candidate_route": "SUPPORT",
                    "baseline_quality": 0.2,
                    "candidate_quality": 0.9,
                },
                "improvement",
            )
        ],
        "regression": [
            EvalCase(
                "REGRESSION",
                {"message": "hello"},
                {"route": "GENERAL"},
                "routing",
                {
                    "baseline_route": "GENERAL",
                    "candidate_route": "GENERAL",
                    "baseline_quality": 0.8,
                    "candidate_quality": 0.8,
                },
                "regression",
            )
        ],
        "holdout": [
            EvalCase(
                "HOLDOUT",
                {"message": "unseen support request"},
                {"route": "SUPPORT"},
                "routing",
                {
                    "baseline_route": "SUPPORT",
                    "candidate_route": "SUPPORT",
                    "baseline_quality": 0.8,
                    "candidate_quality": 0.8,
                },
                "holdout",
            )
        ],
    }


class EvolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.change = EvolutionCandidate(
            "router-v2",
            "skill",
            "router",
            "1",
            "2",
            "skill",
        )
        self.policy = EvolutionPolicy(
            objectives=(
                MetricObjective(
                    "quality",
                    "trace.fields.quality",
                    minimum_improvement=0.1,
                ),
            )
        )

    def test_accepts_improvement_without_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ResultStore(root / "evaluation.db") as store:
                result = EvolutionEngine(
                    EvaluationEngine(adapter("1", False), store),
                    EvaluationEngine(adapter("2", True), store),
                    self.policy,
                ).run(self.change, evolution_cases(), experiment_id="accept-test")
                stored = store.get_evolution("accept-test")
            write_evolution_artifacts(result, root / "out")

            self.assertEqual(result["decision"], "accept")
            self.assertEqual(stored["decision"], "accept")
            self.assertEqual(
                result["comparisons"]["improvement"]["hard_pass_rate_delta"], 1
            )
            self.assertTrue((root / "out" / "evolution_report.md").exists())

    def test_rolls_back_holdout_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ResultStore(root / "evaluation.db") as store:
                result = EvolutionEngine(
                    EvaluationEngine(adapter("1", False), store),
                    EvaluationEngine(adapter("2", True, break_holdout=True), store),
                    self.policy,
                ).run(self.change, evolution_cases(), experiment_id="rollback-test")

            self.assertEqual(result["decision"], "rollback")
            self.assertIn("holdout: hard pass rate exceeded", " ".join(result["reasons"]))

    def test_strict_policy_rejects_an_improved_but_still_failing_holdout(self) -> None:
        datasets = evolution_cases()
        holdout = datasets["holdout"][0]
        datasets["holdout"] = [
            EvalCase(
                holdout.case_id,
                holdout.payload,
                holdout.expected,
                holdout.scenario,
                {
                    **holdout.metadata,
                    "baseline_route": "OTHER",
                    "candidate_route": "OTHER",
                },
                holdout.suite,
            )
        ]
        policy = EvolutionPolicy(
            require_holdout_pass=True,
            objectives=self.policy.objectives,
        )
        with tempfile.TemporaryDirectory() as directory:
            with ResultStore(Path(directory) / "evaluation.db") as store:
                result = EvolutionEngine(
                    EvaluationEngine(adapter("1", False), store),
                    EvaluationEngine(adapter("2", True), store),
                    policy,
                ).run(self.change, datasets, experiment_id="strict-holdout")

        self.assertEqual(result["decision"], "rollback")
        self.assertIn("holdout: candidate has hard failures", result["reasons"])

    def test_scenario_gate_blocks_a_small_regression_hidden_by_the_total_rate(self) -> None:
        datasets = evolution_cases()
        base_holdout = datasets["holdout"][0]
        datasets["holdout"] = [
            EvalCase(
                f"GENERAL-{index}",
                base_holdout.payload,
                base_holdout.expected,
                "general",
                {**base_holdout.metadata, "break_candidate": False},
                "holdout",
            )
            for index in range(10)
        ] + [
            EvalCase(
                "HIGH-RISK",
                base_holdout.payload,
                base_holdout.expected,
                "high-risk",
                {**base_holdout.metadata, "break_candidate": True},
                "holdout",
            )
        ]
        policy = EvolutionPolicy(
            max_holdout_hard_pass_drop=0.1,
            objectives=self.policy.objectives,
            scenario_gates=(
                ScenarioGate(
                    "high-risk",
                    roles=("holdout",),
                    minimum_hard_pass_rate=1.0,
                ),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ResultStore(root / "evaluation.db") as store:
                result = EvolutionEngine(
                    EvaluationEngine(adapter("1", False), store),
                    EvaluationEngine(adapter("2", True, break_holdout=True), store),
                    policy,
                ).run(self.change, datasets, experiment_id="scenario-gate")
            write_evolution_artifacts(result, root / "out")
            report = (root / "out" / "evolution_report.md").read_text("utf-8")

        self.assertGreater(result["comparisons"]["holdout"]["hard_pass_rate_delta"], -0.1)
        self.assertEqual(result["decision"], "rollback")
        self.assertIn(
            "holdout/high-risk: hard pass rate is below the configured minimum",
            result["reasons"],
        )
        self.assertIn("| holdout | high-risk | 1 |", report)


if __name__ == "__main__":
    unittest.main()
