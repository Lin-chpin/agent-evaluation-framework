from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import EvalCase, NormalizedTrace, ProjectAdapter, Rule
from agent_eval.engine import EvaluationEngine
from agent_eval.store import ResultStore


def adapter(name: str = "integrity") -> ProjectAdapter:
    return ProjectAdapter(
        name,
        lambda case, _: {"trace_id": case.case_id, "route": case.payload["route"]},
        lambda handle, _: NormalizedTrace(
            handle["trace_id"], handle, fields={"route": handle["route"]}
        ),
        (Rule("route", "fields.route", expected="route"),),
        (),
    )


def case(case_id: str, route: str) -> EvalCase:
    return EvalCase(case_id, {"route": route}, {"route": route}, suite="integrity")


class RunIntegrityTest(unittest.TestCase):
    def test_rejects_empty_suite_before_creating_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ResultStore(
            Path(directory) / "evaluation.db"
        ) as store:
            with self.assertRaisesRegex(ValueError, "must not be empty"):
                EvaluationEngine(adapter(), store).run_suite([], "integrity", run_id="empty")
            count = store.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

        self.assertEqual(count, 0)

    def test_rejects_duplicate_case_ids_before_creating_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ResultStore(
            Path(directory) / "evaluation.db"
        ) as store:
            with self.assertRaisesRegex(ValueError, "duplicate case_id"):
                EvaluationEngine(adapter(), store).run_suite(
                    [case("DUP", "A"), case("DUP", "B")],
                    "integrity",
                    run_id="duplicate",
                )
            count = store.connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]

        self.assertEqual(count, 0)

    def test_resume_rejects_changed_or_missing_cases(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ResultStore(
            Path(directory) / "evaluation.db"
        ) as store:
            engine = EvaluationEngine(adapter(), store)
            engine.run_suite([case("A", "A"), case("B", "B")], "integrity", run_id="resume")
            with self.assertRaisesRegex(ValueError, "changed case_id: A"):
                engine.run_suite(
                    [case("A", "CHANGED"), case("B", "B")],
                    "integrity",
                    run_id="resume",
                    resume=True,
                )
            with self.assertRaisesRegex(ValueError, "missing case_id: B"):
                engine.run_suite(
                    [case("A", "A")], "integrity", run_id="resume", resume=True
                )

    def test_resume_allows_append_only_cases(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ResultStore(
            Path(directory) / "evaluation.db"
        ) as store:
            engine = EvaluationEngine(adapter(), store)
            engine.run_suite([case("A", "A")], "integrity", run_id="append")
            result = engine.run_suite(
                [case("A", "A"), case("B", "B")],
                "integrity",
                run_id="append",
                resume=True,
            )

        self.assertEqual(result["case_count"], 2)
        self.assertEqual([item["case"]["case_id"] for item in result["results"]], ["A", "B"])

    def test_resume_rejects_changed_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ResultStore(
            Path(directory) / "evaluation.db"
        ) as store:
            EvaluationEngine(adapter(), store).run_suite(
                [case("A", "A")], "integrity", run_id="identity"
            )
            with self.assertRaisesRegex(ValueError, "run identity"):
                EvaluationEngine(adapter("other"), store).run_suite(
                    [case("A", "A")], "integrity", run_id="identity", resume=True
                )


if __name__ == "__main__":
    unittest.main()
