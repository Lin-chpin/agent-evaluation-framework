from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import EvalCase, NormalizedTrace, ProjectAdapter, Rule, RunContext, TraceEvent
from agent_eval.engine import EvaluationEngine
from agent_eval.reporting import write_run_artifacts
from agent_eval.store import ResultStore


class FrameworkTest(unittest.TestCase):
    def test_hard_gate_review_and_few_shot(self) -> None:
        def call_agent(case: EvalCase, context: RunContext) -> dict:
            return {
                "trace_id": f"{context.run_id}-{case.case_id}",
                "output": case.payload["message"],
                "route": case.metadata.get("actual_route", "ECHO"),
            }

        def read_trace(handle: dict, _: EvalCase) -> NormalizedTrace:
            return NormalizedTrace(
                trace_id=handle["trace_id"],
                final_output=handle["output"],
                events=(TraceEvent("EchoAgent", "reply"),),
                fields={"route": handle["route"]},
                target_type="skill",
                target_id="echo",
                target_version="1",
            )

        adapter = ProjectAdapter(
            "test",
            call_agent,
            read_trace,
            hard_gates=(Rule("route", "fields.route", expected="route", suspected_modules=("Router",)),),
            soft_quality=(),
        )
        cases = [
            EvalCase("PASS", {"message": "ok"}, {"route": "ECHO"}, "direct", suite="smoke"),
            EvalCase(
                "FAIL",
                {"message": "bad"},
                {"route": "ECHO"},
                "routing",
                {"actual_route": "OTHER"},
                "smoke",
            ),
        ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ResultStore(root / "evaluation.db") as store:
                engine = EvaluationEngine(adapter, store, collect_few_shot=True)
                summary = engine.run_suite(cases, "smoke", run_id="test-run")
                store.save_review("test-run", "FAIL", "confirmed_badcase", "route mismatch")
                self.assertEqual(len(store.list_reviewed_results("confirmed_badcase")), 1)
            write_run_artifacts(summary, root / "out")

            self.assertEqual(summary["hard_failures"], 1)
            self.assertEqual(summary["status"], "failed")
            self.assertTrue((root / "out" / "report.md").exists())
            self.assertTrue((root / "out" / "scenario_stats.json").exists())
            candidates = (root / "out" / "few_shot_candidates.jsonl").read_text("utf-8")
            self.assertIn('"source_case_id": "PASS"', candidates)

            regression_case = EvalCase(
                "REGRESSION-FAIL",
                {"message": "bad"},
                {"route": "ECHO"},
                "routing",
                {"actual_route": "OTHER"},
                "regression",
            )
            with ResultStore(root / "release.db") as store:
                release = EvaluationEngine(adapter, store).run_release(
                    [("regression", [regression_case]), ("smoke", [cases[0]])],
                    release_id="release-test",
                )
            self.assertEqual(release["status"], "failed")
            self.assertEqual(len(release["stages"]), 1)


if __name__ == "__main__":
    unittest.main()
