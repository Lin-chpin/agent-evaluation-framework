from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agent_eval import EvalCase, RunContext
from integrations.ai_health_assistant.isolated_runtime import IsolatedHealthRuntime


def load_module():
    path = ROOT / "integrations" / "ai_health_assistant" / "prompt_auto_evolution.py"
    spec = importlib.util.spec_from_file_location("ai_health_prompt_evolution_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AiHealthPromptEvolutionTest(unittest.TestCase):
    def test_runtime_removes_derived_version_id_from_sandbox_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "prompt_versions.json"
            target = root / "sandbox" / "prompt_versions.json"
            target.parent.mkdir()
            source.write_text(
                json.dumps(
                    {
                        "versions": {
                            "PLANNER": [
                                {
                                    "version": 1,
                                    "versionId": "PLANNER_v1",
                                    "content": "prompt",
                                }
                            ]
                        },
                        "activeVersions": {"PLANNER": 1},
                    }
                ),
                encoding="utf-8",
            )
            original = source.read_text(encoding="utf-8")
            runtime = IsolatedHealthRuntime()
            runtime.project_root = root

            runtime._copy_prompt_versions(target)

            copied = json.loads(target.read_text(encoding="utf-8"))
            self.assertNotIn("versionId", copied["versions"]["PLANNER"][0])
            self.assertEqual(source.read_text(encoding="utf-8"), original)

    def test_candidate_runs_in_isolated_runtime_with_prompt_identity(self) -> None:
        module = load_module()
        artifact = ROOT / "integrations" / "ai_health_assistant" / "benchmarks" / "test.prompt.txt"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text("planner {user_input}", encoding="utf-8")
        case = EvalCase.from_dict(
            {
                "id": "HAT-TEST",
                "payload": {"message": "头痛"},
                "expected": {"route": "MEDICAL"},
            },
            "improvement",
        )
        raw = {
            "caseId": "HAT-TEST",
            "requestId": "trace-1",
            "detectedRoute": "MEDICAL",
            "executionPath": ["Planner", "Route[MEDICAL]"],
            "finalAnswer": "建议就医",
        }

        try:
            with patch.object(module._RUNTIME, "evaluate", return_value={"HAT-TEST": raw}):
                adapter = module.build_adapter(artifact, "candidate-v2")
                handle = adapter.call_agent(case, RunContext("run", "improvement", "online", 1, 30))
                trace = adapter.read_trace(handle, case)
        finally:
            artifact.unlink(missing_ok=True)

        self.assertEqual(trace.target_type, "prompt")
        self.assertEqual(trace.target_id, "PLANNER")
        self.assertEqual(trace.target_version, "candidate-v2")


if __name__ == "__main__":
    unittest.main()
