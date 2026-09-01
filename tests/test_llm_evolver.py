from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import EvolutionDiagnosis, RetryableEvolverError
from agent_eval.llm import OpenAICompatibleTextEvolver, _parse_json_object


class StubClient:
    def __init__(self) -> None:
        self.responses = [
            {
                "summary": "missing password routing",
                "evidence_case_ids": ["IMPROVE"],
                "suspected_modules": ["Router"],
                "constraints": ["preserve billing"],
            },
            {
                "candidates": [
                    {
                        "summary": "extend keywords",
                        "complete_content": "billing,password",
                    }
                ]
            },
        ]

    def request_json(self, _: str) -> dict:
        return self.responses.pop(0)


class DirectoryStubClient:
    model = "stub"

    def request_json(self, _: str) -> dict:
        return {
            "candidates": [
                {
                    "summary": "update repository",
                    "operations": [
                        {"operation": "write", "path": "routes.txt", "content": "billing,password"},
                        {"operation": "move", "path": "old.txt", "destination": "archive/old.txt"},
                        {"operation": "delete", "path": "unused.txt"},
                    ],
                }
            ]
        }


class InvalidDirectoryStubClient:
    model = "stub"

    def request_json(self, _: str) -> dict:
        return {
            "candidates": [
                {
                    "summary": "escape repository",
                    "operations": [
                        {"operation": "write", "path": "../outside.txt", "content": "bad"}
                    ],
                }
            ]
        }


class LlmEvolverTest(unittest.TestCase):
    def test_extracts_json_object_from_model_wrapping(self) -> None:
        wrapped = "Here is the result:\n```json\n{\"summary\": \"ok\"}\n```"

        self.assertEqual(_parse_json_object(wrapped), {"summary": "ok"})

    def test_turns_model_json_into_diagnosis_and_candidate(self) -> None:
        evolver = OpenAICompatibleTextEvolver(StubClient(), "skill", "router")
        diagnosis = evolver.diagnose(
            {
                "run_id": "run",
                "results": [
                    {
                        "case": {"case_id": "IMPROVE"},
                        "hard_pass": False,
                        "soft_warning_count": 0,
                    }
                ],
            }
        )
        candidates = evolver.generate_candidates(diagnosis, "billing", 1)

        self.assertEqual(diagnosis.evidence_case_ids, ("IMPROVE",))
        self.assertEqual(candidates[0].content, "billing,password")
        self.assertEqual(candidates[0].metadata["generator"], "openai-compatible")

    def test_turns_directory_json_into_file_operations(self) -> None:
        evolver = OpenAICompatibleTextEvolver(DirectoryStubClient(), "agent", "router")
        candidates = evolver.generate_candidates(
            EvolutionDiagnosis("update repository", "agent", "router"),
            {"routes.txt": "billing", "old.txt": "old", "unused.txt": "unused"},
            1,
        )

        self.assertEqual([item.operation for item in candidates[0].operations], ["write", "move", "delete"])
        self.assertEqual(candidates[0].operations[1].destination, "archive/old.txt")

    def test_rejects_unsafe_directory_operation_from_model(self) -> None:
        evolver = OpenAICompatibleTextEvolver(InvalidDirectoryStubClient(), "agent", "router")
        with self.assertRaises(RetryableEvolverError):
            evolver.generate_candidates(
                EvolutionDiagnosis("update repository", "agent", "router"),
                {"routes.txt": "billing"},
                1,
            )


if __name__ == "__main__":
    unittest.main()
