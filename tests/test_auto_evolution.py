from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import (
    AutoEvolutionAdapter,
    EvalCase,
    EvolutionBudget,
    EvolutionDiagnosis,
    EvolutionPolicy,
    MetricObjective,
    NormalizedTrace,
    ProjectAdapter,
    RetryableEvolverError,
    Rule,
    RunContext,
    TextCandidate,
    TraceEvent,
)
from agent_eval.auto_evolution import AutoEvolutionLoop
from agent_eval.reporting import write_auto_evolution_artifacts
from agent_eval.store import ResultStore
from agent_eval.workspace import TextArtifactWorkspace


def build_adapter(artifact: Path, version: str) -> ProjectAdapter:
    keywords = set(artifact.read_text(encoding="utf-8").strip().split(","))

    def call_agent(case: EvalCase, context: RunContext) -> dict:
        message = case.payload["message"]
        route = "SUPPORT" if any(keyword in message for keyword in keywords) else "GENERAL"
        return {
            "trace_id": f"{context.run_id}-{case.case_id}",
            "route": route,
            "quality": 0.9 if route == case.expected["route"] else 0.2,
        }

    def read_trace(handle: dict, _: EvalCase) -> NormalizedTrace:
        return NormalizedTrace(
            handle["trace_id"],
            {"route": handle["route"]},
            (TraceEvent("Router", "route", duration_ms=2),),
            {"route": handle["route"], "quality": handle["quality"]},
            target_type="skill",
            target_id="router",
            target_version=version,
        )

    return ProjectAdapter(
        f"router-{version}",
        call_agent,
        read_trace,
        (Rule("route", "fields.route", expected="route"),),
        (),
    )


def cases() -> dict[str, list[EvalCase]]:
    return {
        "improvement": [
            EvalCase(
                "IMPROVE",
                {"message": "password reset"},
                {"route": "SUPPORT"},
                "routing",
                suite="improvement",
            )
        ],
        "regression": [
            EvalCase(
                "REGRESSION",
                {"message": "hello"},
                {"route": "GENERAL"},
                "routing",
                suite="regression",
            )
        ],
        "holdout": [
            EvalCase(
                "HOLDOUT",
                {"message": "billing issue"},
                {"route": "SUPPORT"},
                "routing",
                suite="holdout",
            )
        ],
    }


class AutoEvolutionTest(unittest.TestCase):
    def test_resume_rejects_changed_frozen_datasets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "router.skill"
            baseline.write_text("billing", encoding="utf-8")
            adapter = AutoEvolutionAdapter(
                "router-auto",
                baseline,
                "1",
                "skill",
                "router",
                lambda _: EvolutionDiagnosis("missing route", "skill", "router"),
                lambda _diagnosis, current, _round: (
                    TextCandidate("good", "2", current + ",password", "add password"),
                ),
                build_adapter,
            )
            workspace = TextArtifactWorkspace(root / "workspaces")
            datasets = cases()
            with ResultStore(root / "evaluation.db") as store:
                loop = AutoEvolutionLoop(store, workspace)
                loop.run(adapter, datasets, loop_id="frozen")
                changed = {role: list(values) for role, values in datasets.items()}
                changed["holdout"].append(
                    EvalCase(
                        "NEW",
                        {"message": "billing"},
                        {"route": "SUPPORT"},
                        suite="holdout",
                    )
                )
                with self.assertRaisesRegex(ValueError, "frozen datasets"):
                    loop.run(adapter, changed, loop_id="frozen", resume=True)

    def test_continues_after_partial_acceptance_until_improvement_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "router.skill"
            baseline.write_text("billing", encoding="utf-8")
            datasets = cases()
            datasets["improvement"] = [
                EvalCase(
                    "PASSWORD",
                    {"message": "password reset"},
                    {"route": "SUPPORT"},
                    "routing",
                    suite="improvement",
                ),
                EvalCase(
                    "REFUND",
                    {"message": "refund request"},
                    {"route": "SUPPORT"},
                    "routing",
                    suite="improvement",
                ),
            ]

            def generate(_: EvolutionDiagnosis, current: str, round_number: int):
                keyword = "password" if round_number == 1 else "refund"
                return (
                    TextCandidate(
                        f"round-{round_number}",
                        str(round_number + 1),
                        current + "," + keyword,
                        f"add {keyword}",
                    ),
                )

            adapter = AutoEvolutionAdapter(
                "router-auto",
                baseline,
                "1",
                "skill",
                "router",
                lambda _: EvolutionDiagnosis("missing routes", "skill", "router"),
                generate,
                build_adapter,
            )
            with ResultStore(root / "evaluation.db") as store:
                result = AutoEvolutionLoop(
                    store,
                    TextArtifactWorkspace(root / "workspaces"),
                ).run(
                    adapter,
                    datasets,
                    budget=EvolutionBudget(max_rounds=2, max_candidates_per_round=1),
                    loop_id="two-rounds",
                )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["current_version"], "3")
            self.assertEqual(len(result["rounds"]), 2)
            self.assertEqual(
                [round_result["candidates"][0]["evaluation"]["decision"] for round_result in result["rounds"]],
                ["accept", "accept"],
            )

    def test_retries_one_retryable_evolver_response_within_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "router.skill"
            baseline.write_text("billing", encoding="utf-8")
            diagnosis_attempts = 0

            def diagnose(_: dict) -> EvolutionDiagnosis:
                nonlocal diagnosis_attempts
                diagnosis_attempts += 1
                if diagnosis_attempts == 1:
                    raise RetryableEvolverError("invalid model JSON")
                return EvolutionDiagnosis("missing password routing", "skill", "router")

            adapter = AutoEvolutionAdapter(
                "router-auto",
                baseline,
                "1",
                "skill",
                "router",
                diagnose,
                lambda *_: (TextCandidate("good", "2", "billing,password", "extend"),),
                build_adapter,
            )
            with ResultStore(root / "evaluation.db") as store:
                result = AutoEvolutionLoop(
                    store, TextArtifactWorkspace(root / "workspaces")
                ).run(
                    adapter,
                    cases(),
                    EvolutionBudget(1, 1, max_evolver_calls=3),
                    loop_id="retry-test",
                )

            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["usage"]["evolver_calls"], 3)
            self.assertEqual(result["usage"]["evolver_retries"], 1)

    def test_rejects_regression_then_accepts_safe_text_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "router.skill"
            baseline.write_text("billing", encoding="utf-8")

            def diagnose(summary: dict) -> EvolutionDiagnosis:
                failed = tuple(
                    result["case"]["case_id"]
                    for result in summary["results"]
                    if not result["hard_pass"]
                )
                return EvolutionDiagnosis("missing password routing", "skill", "router", failed)

            def generate(
                _: EvolutionDiagnosis, current: str, __: int
            ) -> tuple[TextCandidate, ...]:
                return (
                    TextCandidate("bad", "2-bad", "password", "replace baseline behavior"),
                    TextCandidate("good", "2", current + ",password", "extend baseline behavior"),
                )

            adapter = AutoEvolutionAdapter(
                "router-auto",
                baseline,
                "1",
                "skill",
                "router",
                diagnose,
                generate,
                build_adapter,
            )
            policy = EvolutionPolicy(
                objectives=(
                    MetricObjective(
                        "quality",
                        "trace.fields.quality",
                        minimum_improvement=0.1,
                    ),
                )
            )
            with ResultStore(root / "evaluation.db") as store:
                result = AutoEvolutionLoop(
                    store,
                    TextArtifactWorkspace(root / "workspaces"),
                    policy,
                ).run(adapter, cases(), EvolutionBudget(1, 2), loop_id="loop-test")
            write_auto_evolution_artifacts(result, root / "out")

            decisions = [
                candidate["evaluation"]["decision"]
                for candidate in result["rounds"][0]["candidates"]
            ]
            self.assertEqual(decisions, ["rollback", "accept"])
            self.assertEqual(result["status"], "completed")
            self.assertEqual(result["current_version"], "2")
            self.assertEqual(result["datasets"]["improvement"]["case_count"], 1)
            self.assertEqual(len(result["datasets"]["improvement"]["sha256"]), 64)
            self.assertEqual(baseline.read_text(encoding="utf-8"), "billing")
            self.assertEqual(
                Path(result["current_artifact"]).read_text(encoding="utf-8"),
                "billing,password",
            )
            self.assertTrue((root / "out" / "auto_evolution_report.md").exists())

    def test_stops_before_candidate_generation_at_evolver_call_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "router.skill"
            baseline.write_text("billing", encoding="utf-8")

            adapter = AutoEvolutionAdapter(
                "router-auto",
                baseline,
                "1",
                "skill",
                "router",
                lambda _: EvolutionDiagnosis("missing route", "skill", "router"),
                lambda *_: self.fail("candidate generation must not exceed the budget"),
                build_adapter,
            )
            workspace = TextArtifactWorkspace(root / "workspaces")
            with ResultStore(root / "evaluation.db") as store:
                result = AutoEvolutionLoop(store, workspace).run(
                    adapter,
                    cases(),
                    EvolutionBudget(1, 1, max_evolver_calls=1),
                    loop_id="budget-test",
                )

            self.assertEqual(result["status"], "evolver_call_budget_exhausted")
            self.assertEqual(result["usage"]["evolver_calls"], 1)
            self.assertTrue((root / "workspaces" / "budget-test" / "checkpoint.json").is_file())

    def test_time_budget_stops_before_the_first_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "router.skill"
            baseline.write_text("billing", encoding="utf-8")
            adapter = AutoEvolutionAdapter(
                "router-auto",
                baseline,
                "1",
                "skill",
                "router",
                lambda _: self.fail("diagnosis must not run after time is exhausted"),
                lambda *_: (),
                build_adapter,
            )
            with ResultStore(root / "evaluation.db") as store:
                result = AutoEvolutionLoop(
                    store, TextArtifactWorkspace(root / "workspaces")
                ).run(
                    adapter,
                    cases(),
                    EvolutionBudget(1, 1, max_elapsed_seconds=0.000001),
                    loop_id="time-test",
                )

            self.assertEqual(result["status"], "time_budget_exhausted")

    def test_resume_reuses_the_staged_candidate_after_adapter_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "router.skill"
            baseline.write_text("billing", encoding="utf-8")
            fail_candidate_once = True

            def diagnose(_: dict) -> EvolutionDiagnosis:
                return EvolutionDiagnosis("missing password routing", "skill", "router")

            def generate(
                _: EvolutionDiagnosis, current: str, __: int
            ) -> tuple[TextCandidate, ...]:
                return (TextCandidate("good", "2", current + ",password", "extend"),)

            def flaky_builder(artifact: Path, version: str) -> ProjectAdapter:
                nonlocal fail_candidate_once
                if version == "2" and fail_candidate_once:
                    fail_candidate_once = False
                    raise RuntimeError("temporary candidate startup failure")
                return build_adapter(artifact, version)

            adapter = AutoEvolutionAdapter(
                "router-auto",
                baseline,
                "1",
                "skill",
                "router",
                diagnose,
                generate,
                flaky_builder,
            )
            workspace = TextArtifactWorkspace(root / "workspaces")
            with ResultStore(root / "evaluation.db") as store:
                loop = AutoEvolutionLoop(store, workspace)
                failed = loop.run(
                    adapter,
                    cases(),
                    EvolutionBudget(1, 1, max_evolver_calls=2),
                    loop_id="resume-test",
                )
                resumed = loop.run(
                    adapter,
                    cases(),
                    EvolutionBudget(1, 1, max_evolver_calls=4),
                    loop_id="resume-test",
                    resume=True,
                )

            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["failed_phase"], "candidate_evaluation")
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(resumed["current_version"], "2")
            self.assertEqual(resumed["usage"]["evolver_calls"], 4)


if __name__ == "__main__":
    unittest.main()
