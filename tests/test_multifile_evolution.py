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
    NormalizedTrace,
    ProjectAdapter,
    Rule,
    RunContext,
    TextCandidate,
    TextFileOperation,
)
from agent_eval.auto_evolution import AutoEvolutionLoop
from agent_eval.store import ResultStore
from agent_eval.workspace import TextArtifactWorkspace


def build_adapter(artifact: Path, version: str) -> ProjectAdapter:
    keywords = set((artifact / "routes.txt").read_text(encoding="utf-8").split(","))
    mode = (artifact / "config" / "mode.txt").read_text(encoding="utf-8")

    def call_agent(case: EvalCase, context: RunContext) -> dict[str, str]:
        route = "SUPPORT" if any(word in case.payload["message"] for word in keywords) else "GENERAL"
        if mode != "safe":
            route = "SUPPORT"
        return {"trace_id": f"{context.run_id}-{case.case_id}", "route": route}

    return ProjectAdapter(
        f"multi-file-{version}",
        call_agent,
        lambda handle, _: NormalizedTrace(
            handle["trace_id"],
            {"route": handle["route"]},
            fields={"route": handle["route"]},
            target_type="agent",
            target_id="router",
            target_version=version,
        ),
        (Rule("route", "fields.route", expected="route"),),
        (),
    )


class MultiFileEvolutionTest(unittest.TestCase):
    def test_rolls_back_bad_directory_candidate_and_accepts_safe_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "repository"
            (baseline / "config").mkdir(parents=True)
            (baseline / "routes.txt").write_text("billing", encoding="utf-8")
            (baseline / "config" / "mode.txt").write_text("safe", encoding="utf-8")
            (baseline / "legacy.txt").write_text("legacy", encoding="utf-8")
            (baseline / "obsolete.txt").write_text("obsolete", encoding="utf-8")
            datasets = {
                "improvement": [EvalCase("I", {"message": "password"}, {"route": "SUPPORT"}, suite="improvement")],
                "regression": [EvalCase("R", {"message": "hello"}, {"route": "GENERAL"}, suite="regression")],
                "holdout": [EvalCase("H", {"message": "billing"}, {"route": "SUPPORT"}, suite="holdout")],
            }

            def generate(_: EvolutionDiagnosis, current, __: int):
                self.assertIsInstance(current, dict)
                return (
                    TextCandidate(
                        "bad",
                        "2-bad",
                        "",
                        "unsafe mode",
                        operations=(
                            TextFileOperation("write", "routes.txt", "billing,password"),
                            TextFileOperation("write", "config/mode.txt", "unsafe"),
                        ),
                    ),
                    TextCandidate(
                        "good",
                        "2",
                        "",
                        "safe extension",
                        operations=(
                            TextFileOperation("write", "routes.txt", "billing,password"),
                            TextFileOperation("move", "legacy.txt", destination="archive/legacy.txt"),
                            TextFileOperation("delete", "obsolete.txt"),
                        ),
                    ),
                )

            adapter = AutoEvolutionAdapter(
                "multi-file-auto",
                baseline,
                "1",
                "agent",
                "router",
                lambda _: EvolutionDiagnosis("missing password", "agent", "router"),
                generate,
                build_adapter,
            )
            with ResultStore(root / "evaluation.db") as store:
                result = AutoEvolutionLoop(store, TextArtifactWorkspace(root / "workspaces")).run(
                    adapter,
                    datasets,
                    EvolutionBudget(1, 2),
                    loop_id="multi-file",
                )

            self.assertEqual(
                [item["evaluation"]["decision"] for item in result["rounds"][0]["candidates"]],
                ["rollback", "accept"],
            )
            self.assertEqual((baseline / "routes.txt").read_text(encoding="utf-8"), "billing")
            accepted = Path(result["current_artifact"])
            self.assertEqual((accepted / "routes.txt").read_text(encoding="utf-8"), "billing,password")
            self.assertEqual((accepted / "config" / "mode.txt").read_text(encoding="utf-8"), "safe")
            self.assertEqual((accepted / "archive" / "legacy.txt").read_text(encoding="utf-8"), "legacy")
            self.assertFalse((accepted / "legacy.txt").exists())
            self.assertFalse((accepted / "obsolete.txt").exists())

    def test_rejects_file_outside_candidate_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "repository"
            baseline.mkdir()
            (baseline / "safe.txt").write_text("safe", encoding="utf-8")
            workspace = TextArtifactWorkspace(root / "workspaces")
            workspace.snapshot("escape", baseline)
            with self.assertRaisesRegex(ValueError, "safe forward-slash relative file path"):
                workspace.stage(
                    "escape",
                    1,
                    TextCandidate(
                        "bad",
                        "2",
                        "",
                        "escape",
                        operations=(TextFileOperation("write", "../outside.txt", "bad"),),
                    ),
                    baseline.name,
                    root / "workspaces" / "escape" / "baseline" / baseline.name,
                )

    def test_rejects_conflicting_file_operations_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "repository"
            baseline.mkdir()
            (baseline / "one.txt").write_text("one", encoding="utf-8")
            workspace = TextArtifactWorkspace(root / "workspaces")
            current = workspace.snapshot("conflict", baseline)

            with self.assertRaisesRegex(ValueError, "operations conflict"):
                workspace.stage(
                    "conflict",
                    1,
                    TextCandidate(
                        "bad",
                        "2",
                        "",
                        "conflict",
                        operations=(
                            TextFileOperation("write", "one.txt", "changed"),
                            TextFileOperation("delete", "one.txt"),
                        ),
                    ),
                    baseline.name,
                    current,
                )


if __name__ == "__main__":
    unittest.main()
