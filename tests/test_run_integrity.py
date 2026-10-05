from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            engine = EvaluationEngine(adapter(), store, run_identity="integrity-v1")
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
            engine = EvaluationEngine(adapter(), store, run_identity="integrity-v1")
            engine.run_suite([case("A", "A")], "integrity", run_id="append")
            result = engine.run_suite(
                [case("A", "A"), case("B", "B")],
                "integrity",
                run_id="append",
                resume=True,
            )

        self.assertEqual(result["case_count"], 2)
        self.assertEqual([item["case"]["case_id"] for item in result["results"]], ["A", "B"])

    def test_batched_results_survive_interrupted_run_and_resume(self) -> None:
        cases = [case(str(index), "A") for index in range(8)]
        with tempfile.TemporaryDirectory() as directory, ResultStore(
            Path(directory) / "evaluation.db"
        ) as store:
            engine = EvaluationEngine(adapter(), store, workers=2, run_identity="batch-v1")
            save_cases = store.save_cases
            batches: list[int] = []

            def interrupt_second_batch(results):
                batches.append(len(results))
                if len(batches) == 2:
                    raise RuntimeError("interrupted before commit")
                save_cases(results)

            with patch.object(store, "save_cases", side_effect=interrupt_second_batch):
                with self.assertRaisesRegex(RuntimeError, "interrupted before commit"):
                    engine.run_suite(cases, "integrity", run_id="batch")
            self.assertEqual(batches, [4, 4])
            self.assertEqual(len(store.list_results("batch")), 4)

            resumed = engine.run_suite(cases, "integrity", run_id="batch", resume=True)
            self.assertEqual(resumed["status"], "passed")
            self.assertEqual(resumed["case_count"], 8)

    def test_resume_rejects_changed_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory, ResultStore(
            Path(directory) / "evaluation.db"
        ) as store:
            EvaluationEngine(adapter(), store, run_identity="integrity-v1").run_suite(
                [case("A", "A")], "integrity", run_id="identity"
            )
            with self.assertRaisesRegex(ValueError, "run identity"):
                EvaluationEngine(adapter("other"), store, run_identity="integrity-v1").run_suite(
                    [case("A", "A")], "integrity", run_id="identity", resume=True
                )
            with self.assertRaisesRegex(ValueError, "run identity"):
                EvaluationEngine(adapter(), store, run_identity="integrity-v2").run_suite(
                    [case("A", "A")], "integrity", run_id="identity", resume=True
                )
            with self.assertRaisesRegex(ValueError, "requires a stable run_identity"):
                EvaluationEngine(adapter(), store).run_suite(
                    [case("A", "A")], "integrity", run_id="identity", resume=True
                )

    def test_duplicate_trace_id_is_saved_as_hard_failure(self) -> None:
        shared = ProjectAdapter(
            "shared-trace",
            lambda case, _: {"route": case.payload["route"]},
            lambda handle, _: NormalizedTrace("shared", handle, fields=handle),
            (Rule("route", "fields.route", expected="route"),),
            (),
        )
        with tempfile.TemporaryDirectory() as directory, ResultStore(
            Path(directory) / "evaluation.db"
        ) as store:
            engine = EvaluationEngine(shared, store, run_identity="shared-v1")
            result = engine.run_suite([case("A", "A"), case("B", "B")], "integrity", run_id="traces")
            resumed = engine.run_suite(
                [case("A", "A"), case("B", "B")], "integrity", run_id="traces", resume=True
            )
        self.assertEqual(result["hard_failures"], 2)
        self.assertEqual(resumed["hard_failures"], 2)
        self.assertTrue(all(
            sum(check["name"] == "unique_trace_id" for check in item["checks"]) == 1
            for item in resumed["results"]
        ))


if __name__ == "__main__":
    unittest.main()
