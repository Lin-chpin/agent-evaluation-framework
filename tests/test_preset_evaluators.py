from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval import EvalCase, NormalizedTrace, ProjectAdapter, TraceEvent
from agent_eval.engine import EvaluationEngine
from agent_eval.preset_evaluators import semantic_evaluations, tool_behavior_checks
from agent_eval.reporting import write_run_artifacts
from agent_eval.store import ResultStore


class StubReviewer:
    def __init__(self, verdict="FAIL"):
        self.verdict = verdict
        self.calls = []

    def request_json(self, prompt):
        self.calls.append(prompt)
        return {"verdict": self.verdict, "reason": "example model judgment"}

    def analyze(self, case, traces, checks, collect_few_shot):
        return {"summary": "advisory only", "authoritative": False}


def sample_trace(*, answer="Paris", tools=("search_docs", "summarize"), context="Paris is the capital of France"):
    return NormalizedTrace(
        "trace-1", answer,
        events=tuple(TraceEvent("OpenAITool", tool, fields={"span_type": "function"}) for tool in tools),
        fields={"context": context},
    )


def sample_case(**metadata):
    return EvalCase("demo", {"question": "What is the capital of France?"}, {"answer": "Paris"}, metadata=metadata)


class ToolPresetTests(unittest.TestCase):
    def test_required_forbidden_order_max_and_error_free_pass(self):
        case = sample_case(behavior_evaluators={
            "required_tools": ["search_docs"],
            "forbidden_tools": ["delete_records"],
            "tool_order": ["search_docs", "summarize"],
            "max_tool_calls": 2,
            "no_tool_errors": True,
        })
        checks = tool_behavior_checks(case, sample_trace())
        self.assertEqual(len(checks), 5)
        self.assertTrue(all(check.passed and check.level == "hard" for check in checks))

    def test_failures_are_detected_without_changing_existing_checks(self):
        case = sample_case(behavior_evaluators={
            "required_tools": ["search_docs", "get_customer"],
            "forbidden_tools": ["delete_records"],
            "tool_order": ["summarize", "search_docs"],
            "max_tool_calls": 1,
        })
        checks = tool_behavior_checks(case, sample_trace(tools=("search_docs", "delete_records")))
        self.assertEqual({check.name for check in checks if not check.passed},
                         {"required_tools", "forbidden_tools", "tool_order", "max_tool_calls"})

    def test_recognizes_custom_tagged_events_and_errors(self):
        trace = NormalizedTrace("t", "ok", events=(
            TraceEvent("CustomTool", "execute", status="error", fields={"kind": "tool", "tool_name": "lookup"}),
            TraceEvent("Agent", "reply"),
        ))
        case = sample_case(behavior_evaluators={"required_tools": ["lookup"], "no_tool_errors": True})
        checks = tool_behavior_checks(case, trace)
        self.assertTrue(checks[0].passed)
        self.assertFalse(checks[1].passed)

    def test_invalid_configuration_is_rejected(self):
        for bad in ({"required_tools": "search_docs"}, {"max_tool_calls": True},
                    {"no_tool_errors": "yes"}, {"unrecognized": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                tool_behavior_checks(sample_case(behavior_evaluators=bad), sample_trace())

    def test_unconfigured_presets_are_inert(self):
        self.assertEqual(tool_behavior_checks(sample_case(), sample_trace()), [])

    def test_order_allows_intermediate_calls_and_requires_repeated_occurrences(self):
        case = sample_case(behavior_evaluators={"tool_order": ["search_docs", "search_docs", "summarize"]})
        self.assertTrue(tool_behavior_checks(case, sample_trace(
            tools=("search_docs", "lookup", "search_docs", "summarize")))[0].passed)
        self.assertFalse(tool_behavior_checks(case, sample_trace())[0].passed)

    def test_declared_unrecognized_calls_cannot_pass_as_zero_calls(self):
        trace = NormalizedTrace("legacy", "ok", events=(
            TraceEvent("LegacyAdapter", "tool_call", fields={"name": "delete_records"}),
        ), fields={"tool_call_count": 1})
        case = sample_case(behavior_evaluators={"forbidden_tools": ["delete_records"], "max_tool_calls": 0})
        checks = tool_behavior_checks(case, trace)
        self.assertEqual(checks[0].name, "tool_trace_coverage")
        self.assertFalse(checks[0].passed)
        self.assertEqual((checks[0].actual, checks[0].expected), (0, 1))

    def test_bfcl_adapter_calls_are_recognized_and_blocked(self):
        import runpy
        build_adapter = runpy.run_path(str(Path(__file__).parents[1] / "scripts/verify_bfcl_adapter.py"))["build_adapter"]
        handle = {"trace_id": "bfcl", "tool_calls": [{"name": "weather.get", "arguments": {"city": "Boston"}}]}
        adapter = build_adapter(lambda case, context: handle)
        case = EvalCase("bfcl", {}, {"ground_truth": [{"weather.get": {"city": ["Boston"]}}]},
            metadata={"behavior_evaluators": {"required_tools": ["weather.get"],
                      "forbidden_tools": ["weather.get"], "max_tool_calls": 0}})
        with tempfile.TemporaryDirectory() as directory, ResultStore(Path(directory) / "bfcl.db") as store:
            summary = EvaluationEngine(adapter, store).run_suite([case], "smoke")
        row = summary["results"][0]
        self.assertEqual(summary["hard_failures"], 1)
        checks = {check["name"]: check for check in row["checks"]}
        self.assertTrue(checks["required_tools"]["passed"])
        self.assertFalse(checks["forbidden_tools"]["passed"])
        self.assertFalse(checks["max_tool_calls"]["passed"])
        self.assertEqual(checks["max_tool_calls"]["actual"], 1)

    def test_forbidden_calls_in_later_trials_block_release_and_keep_trial_evidence(self):
        traces = iter((sample_trace(), NormalizedTrace("later", "ok", events=(TraceEvent("Tool", "delete_records"),))))
        adapter = ProjectAdapter("trial-test", lambda case, context: None, lambda handle, case: next(traces), (), ())
        case = sample_case(consistency_runs=2, behavior_evaluators={"forbidden_tools": ["delete_records"]})
        with tempfile.TemporaryDirectory() as directory, ResultStore(Path(directory) / "trials.db") as store:
            release = EvaluationEngine(adapter, store).run_release([("smoke", [case]), ("full", [case])])
        self.assertEqual(release["status"], "failed")
        self.assertEqual(len(release["stages"]), 1)
        checks = [check for check in release["stages"][0]["results"][0]["checks"]
                  if check["name"] == "forbidden_tools"]
        self.assertEqual([check["passed"] for check in checks], [True, False])
        self.assertIn("trial 2, trace later", checks[1]["message"])
        self.assertEqual(checks[1]["actual"], ["delete_records"])


class SemanticPresetTests(unittest.TestCase):
    def test_three_semantic_presets_use_only_available_evidence(self):
        reviewer = StubReviewer("PASS")
        case = sample_case(semantic_evaluators=[
            "answer_relevance", "reference_correctness", "groundedness",
        ])
        results = semantic_evaluations(case, sample_trace(), reviewer)
        self.assertEqual([row["name"] for row in results],
                         ["answer_relevance", "reference_correctness", "groundedness"])
        self.assertTrue(all(row["verdict"] == "PASS" and row["authoritative"] is False for row in results))
        self.assertEqual(len(reviewer.calls), 3)
        self.assertIn("Paris is the capital of France", reviewer.calls[2])

    def test_model_absent_or_missing_gold_never_passes(self):
        case = sample_case(semantic_evaluators=["reference_correctness"])
        result = semantic_evaluations(case, sample_trace(), None)[0]
        self.assertEqual(result["verdict"], "UNAVAILABLE")

        missing = EvalCase("no-gold", {"message": "question"}, {}, metadata={"semantic_evaluators": ["reference_correctness"]})
        result = semantic_evaluations(missing, sample_trace(), StubReviewer())[0]
        self.assertEqual(result["verdict"], "UNCERTAIN")

    def test_invalid_model_response_never_passes(self):
        result = semantic_evaluations(
            sample_case(semantic_evaluators=["answer_relevance"]),
            sample_trace(), StubReviewer("passed"),
        )[0]
        self.assertEqual(result["verdict"], "UNCERTAIN")

    def test_unknown_semantic_preset_raises(self):
        with self.assertRaisesRegex(ValueError, "unknown semantic"):
            semantic_evaluations(sample_case(semantic_evaluators=["magic_accuracy"]), sample_trace(), None)

    def test_missing_or_invalid_reason_is_uncertain(self):
        class IncompleteReviewer:
            def request_json(self, prompt):
                return judgment
        case = sample_case(semantic_evaluators=["answer_relevance"])
        for judgment in ({"verdict": "PASS"}, {"verdict": "PASS", "reason": " "},
                         {"verdict": "PASS", "reason": None}, {"verdict": "PASS", "reason": {"text": "ok"}}):
            with self.subTest(judgment=judgment):
                result = semantic_evaluations(case, sample_trace(), IncompleteReviewer())[0]
                self.assertEqual(result["verdict"], "UNCERTAIN")
                self.assertIn("reason", result["reason"])

    def test_unconfigured_semantics_never_call_reviewer(self):
        reviewer = StubReviewer()
        self.assertEqual(semantic_evaluations(sample_case(), sample_trace(), reviewer), [])
        self.assertEqual(reviewer.calls, [])

    def test_advisory_result_survives_diagnostic_overwrite_or_error(self):
        class DiagnosticReviewer(StubReviewer):
            def analyze(self, case, traces, checks, collect_few_shot):
                self.collect_few_shot = collect_few_shot
                if diagnostic_error:
                    raise RuntimeError("offline diagnostic failure")
                return {"semantic_evaluators": [], "summary": "offline", "authoritative": True}
        case = sample_case(semantic_evaluators=["answer_relevance"])
        for diagnostic_error in (False, True):
            with self.subTest(diagnostic_error=diagnostic_error), tempfile.TemporaryDirectory() as directory:
                reviewer = DiagnosticReviewer("FAIL")
                adapter = ProjectAdapter("semantic-trace", lambda case, context: None,
                                         lambda handle, case: sample_trace(), (), ())
                root = Path(directory)
                with ResultStore(root / "db.sqlite") as store:
                    summary = EvaluationEngine(adapter, store, reviewer=reviewer, collect_few_shot=True).run_suite([case], "smoke")
                row = summary["results"][0]
                self.assertTrue(row["hard_pass"])
                self.assertEqual(row["soft_warning_count"], 0)
                self.assertEqual(row["llm_review"]["semantic_evaluators"][0]["verdict"], "FAIL")
                self.assertFalse(row["llm_review"]["authoritative"])
                self.assertFalse(row["few_shot_candidate"])
                self.assertFalse(reviewer.collect_few_shot)
                write_run_artifacts(summary, root / "out")
                queue = [json.loads(line) for line in (root / "out/review_queue.jsonl").read_text("utf-8").splitlines()]
                self.assertEqual(queue[0]["semantic_evaluators"][0]["verdict"], "FAIL")
                self.assertIn("semantic/answer_relevance:FAIL", (root / "out/report.md").read_text("utf-8"))

    def test_diagnostics_cannot_enable_unconfigured_semantic_presets(self):
        class ExtraKeysReviewer(StubReviewer):
            def analyze(self, *args):
                return {"semantic_evaluators": [{"name": "groundedness", "verdict": "FAIL"}]}
        reviewer = ExtraKeysReviewer()
        adapter = ProjectAdapter("no-semantics", lambda case, context: None,
                                 lambda handle, case: sample_trace(), (), ())
        with tempfile.TemporaryDirectory() as directory, ResultStore(Path(directory) / "db.sqlite") as store:
            summary = EvaluationEngine(adapter, store, reviewer=reviewer, collect_few_shot=True).run_suite([sample_case()], "smoke")
        self.assertNotIn("semantic_evaluators", summary["results"][0]["llm_review"])
        self.assertEqual(reviewer.calls, [])

    def test_advisory_failure_enters_review_queue_but_not_quality_gates(self):
        reviewer = StubReviewer("FAIL")
        def caller(case, context):
            return context.run_id
        def reader(handle, case):
            return sample_trace()
        adapter = ProjectAdapter("preset-tests", caller, reader, (), ())
        case = sample_case(semantic_evaluators=["answer_relevance"])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with ResultStore(root / "state.db") as store:
                summary = EvaluationEngine(adapter, store, reviewer=reviewer).run_suite(
                    [case], "smoke", run_id="semantic-demo")
            self.assertEqual(summary["hard_failures"], 0)
            self.assertEqual(summary["soft_warnings"], 0)
            self.assertEqual(summary["results"][0]["llm_review"]["semantic_evaluators"][0]["verdict"], "FAIL")
            write_run_artifacts(summary, root / "out")
            rows = [json.loads(line) for line in (root / "out" / "review_queue.jsonl").read_text().splitlines()]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["semantic_evaluators"][0]["verdict"], "FAIL")

    def test_tool_gate_blocks_when_configured(self):
        adapter = ProjectAdapter("preset-tests", lambda case, context: None,
                                 lambda handle, case: sample_trace(tools=("delete_records",)), (), ())
        case = sample_case(behavior_evaluators={"forbidden_tools": ["delete_records"]})
        with tempfile.TemporaryDirectory() as directory, ResultStore(Path(directory) / "db.sqlite") as store:
            summary = EvaluationEngine(adapter, store).run_suite([case], "smoke")
        self.assertEqual(summary["hard_failures"], 1)
        self.assertTrue(any(check["name"] == "forbidden_tools" and not check["passed"]
                            for check in summary["results"][0]["checks"]))


if __name__ == "__main__":
    unittest.main()
