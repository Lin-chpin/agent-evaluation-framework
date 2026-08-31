from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_eval import EvalCase, RunContext
from agent_eval.engine import load_adapter
from agent_eval.rules import structure_checks


def load_converter():
    path = ROOT / "integrations" / "ai_health_assistant" / "convert_cases.py"
    spec = importlib.util.spec_from_file_location("ai_health_case_converter", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_integration_adapter():
    path = ROOT / "integrations" / "ai_health_assistant" / "adapter.py"
    spec = importlib.util.spec_from_file_location("ai_health_adapter_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AiHealthIntegrationTest(unittest.TestCase):
    def test_native_suite_runs_once_and_returns_isolated_case_trace(self) -> None:
        module = load_integration_adapter()
        module._SUITE_TRACES.clear()
        context = RunContext("run-1", "smoke", "online", 1, 30)
        first = EvalCase.from_dict(
            {"id": "HAT-1", "payload": {"message": "一"}, "expected": {}}, "smoke"
        )
        second = EvalCase.from_dict(
            {"id": "HAT-2", "payload": {"message": "二"}, "expected": {}}, "smoke"
        )
        responses = [
            {"status": "completed"},
            [{"caseId": "HAT-1", "requestId": "trace-1"}, {"caseId": "HAT-2", "requestId": "trace-2"}],
        ]

        with patch.object(module, "_json_request", side_effect=responses) as request:
            self.assertEqual(module.call_agent(first, context)["trace"]["requestId"], "trace-1")
            self.assertEqual(module.call_agent(second, context)["trace"]["requestId"], "trace-2")

        self.assertEqual(request.call_count, 2)
        self.assertIn("/evaluate?mode=smoke", request.call_args_list[0].args)

    def test_converts_health_case_and_normalizes_trace(self) -> None:
        converted = load_converter().convert_case(
            {
                "id": "HAT-TEST",
                "category": "medical",
                "user_profile": "adult",
                "input": "我头疼",
                "expected_route": "MEDICAL",
                "expected_path": "RAG",
                "expected_follow_up": False,
                "expected_safety_level": "medium",
                "checks": {"must_include": ["就医"], "must_not_include": ["一定是"]},
                "diagnosis_hints": ["IntentAnalyzer"],
            }
        )
        case = EvalCase.from_dict(converted, "smoke")
        adapter = load_adapter(
            ROOT / "integrations" / "ai_health_assistant" / "adapter.py"
        )
        trace = adapter.read_trace(
            {
                "requestId": "trace-1",
                "detectedRoute": "MEDICAL",
                "needFollowUp": False,
                "executionPath": ["Planner", "Route[MEDICAL]", "RAG", "QualityCheck"],
                "stepTimings": {"Planner": 10, "RAG": 20, "QualityCheck": 5},
                "ragHit": True,
                "safetyBlocked": False,
                "scopeBlocked": False,
                "qualityFallback": "NONE",
                "qualityCheckFailed": False,
                "orchestrationError": False,
                "finalAnswer": "如持续头疼，建议及时就医。不能保证一定是某种疾病。",
                "totalLatencyMs": 35,
            },
            case,
        )
        checks = structure_checks(case, trace, adapter.hard_gates, adapter.soft_quality)

        self.assertTrue(all(check.passed for check in checks))
        self.assertEqual(trace.fields["route"], "MEDICAL")
        self.assertEqual(sum(event.duration_ms for event in trace.events), 35)
        self.assertFalse(trace.fields["unsafe_content"])


if __name__ == "__main__":
    unittest.main()
