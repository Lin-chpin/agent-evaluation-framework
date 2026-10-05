from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_eval import EvalCase, NormalizedTrace, ProjectAdapter, Rule, RunContext, TraceEvent


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError(f"{path}:{line_number} must contain a JSON object")
        rows.append(value)
    return rows


def _ground_truth_names(ground_truth: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for call in ground_truth:
        if not isinstance(call, Mapping) or len(call) != 1:
            raise ValueError("BFCL ground_truth entries must contain one function name")
        names.extend(str(name) for name in call)
    return names


def convert_cases(
    entries: list[dict[str, Any]],
    answers: list[dict[str, Any]],
    category: str,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    answer_by_id = {str(item.get("id", "")): item for item in answers}
    if len(answer_by_id) != len(answers):
        raise ValueError("BFCL answer file contains duplicate or empty ids")

    converted: list[dict[str, Any]] = []
    for entry in entries[:limit]:
        case_id = str(entry.get("id", "")).strip()
        if not case_id:
            raise ValueError("BFCL question entry requires a non-empty id")
        functions = entry.get("function")
        if not isinstance(functions, list):
            raise ValueError(f"{case_id}: function must be a list")
        answer = answer_by_id.get(case_id)
        if answer is None or not isinstance(answer.get("ground_truth"), list):
            raise ValueError(f"{case_id}: missing ground_truth")
        offered_names = {
            str(function.get("name", ""))
            for function in functions
            if isinstance(function, Mapping)
        }
        expected_names = _ground_truth_names(answer["ground_truth"])
        missing = sorted(set(expected_names) - offered_names)
        if missing:
            raise ValueError(f"{case_id}: answer names not offered by question: {missing}")
        converted.append(
            {
                "id": case_id,
                "input": {
                    "category": category,
                    "question": entry.get("question"),
                    "functions": functions,
                },
                "expected": {"ground_truth": answer["ground_truth"]},
                "scenario": category,
                "metadata": {
                    "benchmark": "BFCL",
                    "category": category,
                    "source_case_id": case_id,
                },
            }
        )
    return converted


def _value_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_value_matches(actual, candidate) for candidate in expected)
    if isinstance(expected, Mapping):
        return (
            isinstance(actual, Mapping)
            and set(actual) == set(expected)
            and all(_value_matches(actual[key], value) for key, value in expected.items())
        )
    return actual == expected


def normalize_tool_calls(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        calls = value.get("tool_calls", value.get("calls"))
        if calls is None and "name" in value:
            calls = [value]
        if calls is None:
            calls = [
                {"name": name, "arguments": arguments}
                for name, arguments in value.items()
            ]
    else:
        calls = value
    if not isinstance(calls, list):
        raise ValueError("agent output must contain a tool_calls list")

    normalized: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, Mapping):
            raise ValueError("each tool call must be an object")
        function = call.get("function", call)
        if not isinstance(function, Mapping):
            raise ValueError("tool call function must be an object")
        name = str(function.get("name", "")).strip()
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not name or not isinstance(arguments, Mapping):
            raise ValueError("tool call requires a name and object arguments")
        normalized.append({"name": name, "arguments": dict(arguments)})
    return normalized


def judge_tool_calls(
    actual_output: Any, ground_truth: list[dict[str, Any]]
) -> tuple[bool, str, list[dict[str, Any]]]:
    actual = normalize_tool_calls(actual_output)
    expected = [
        {"name": name, "arguments": arguments}
        for call in ground_truth
        for name, arguments in call.items()
    ]
    if len(actual) != len(expected):
        return False, f"expected {len(expected)} calls, got {len(actual)}", actual
    for index, (actual_call, expected_call) in enumerate(zip(actual, expected)):
        if actual_call["name"] != expected_call["name"]:
            return (
                False,
                f"call {index}: expected {expected_call['name']}, got {actual_call['name']}",
                actual,
            )
        expected_arguments = expected_call["arguments"]
        actual_arguments = actual_call["arguments"]
        if set(actual_arguments) != set(expected_arguments) or any(
            not _value_matches(actual_arguments[key], value)
            for key, value in expected_arguments.items()
        ):
            return False, f"call {index}: arguments do not match", actual
    return True, "passed", actual


def build_adapter(call_agent: Callable[[EvalCase, RunContext], Any]) -> ProjectAdapter:
    def read_trace(handle: Any, case: EvalCase) -> NormalizedTrace:
        passed, reason, calls = judge_tool_calls(
            handle, list(case.expected["ground_truth"])
        )
        trace_id = str(handle.get("trace_id", "")) if isinstance(handle, Mapping) else ""
        return NormalizedTrace(
            trace_id=trace_id,
            final_output=handle,
            events=tuple(
                TraceEvent(
                    module="BFCLAdapter",
                    action="tool_call",
                    fields={"name": call["name"]},
                )
                for call in calls
            ),
            fields={
                "bfcl_passed": passed,
                "bfcl_reason": reason,
                "tool_call_count": len(calls),
                "tool_names": [call["name"] for call in calls],
                "category": case.scenario,
            },
            target_type="benchmark",
            target_id="BFCL",
            target_version="adapter-phase-a",
            raw={"tool_calls": calls},
        )

    return ProjectAdapter(
        name="bfcl-phase-a",
        call_agent=call_agent,
        read_trace=read_trace,
        hard_gates=(
            Rule(
                name="bfcl_tool_calls",
                actual="fields.bfcl_passed",
                value=True,
                suspected_modules=("BFCLAdapter",),
            ),
        ),
        soft_quality=(),
    )


def self_check(output: Path | None = None) -> None:
    ground_truth = [{"weather.get": {"city": ["Boston"]}}]
    converted = convert_cases(
        [
            {
                "id": "simple_python_0",
                "question": [[{"role": "user", "content": "Weather?"}]],
                "function": [{"name": "weather.get"}],
            }
        ],
        [{"id": "simple_python_0", "ground_truth": ground_truth}],
        "simple_python",
    )
    assert converted[0]["expected"]["ground_truth"] == ground_truth
    expected = EvalCase(
        case_id="self-check",
        payload={},
        expected={"ground_truth": ground_truth},
        scenario="simple_python",
    )

    correct = {"trace_id": "trace-1", "tool_calls": [{"name": "weather.get", "arguments": {"city": "Boston"}}]}
    missing = {"trace_id": "trace-2", "tool_calls": []}
    wrong_parameter = {"trace_id": "trace-3", "tool_calls": [{"name": "weather.get", "arguments": {"city": "Paris"}}]}
    extra_call = {
        "trace_id": "trace-4",
        "tool_calls": [
            {"name": "weather.get", "arguments": {"city": "Boston"}},
            {"name": "weather.get", "arguments": {"city": "Boston"}},
        ],
    }
    assert judge_tool_calls(correct, ground_truth)[0]
    assert not judge_tool_calls(missing, ground_truth)[0]
    assert not judge_tool_calls(wrong_parameter, ground_truth)[0]
    assert not judge_tool_calls(extra_call, ground_truth)[0]

    adapter = build_adapter(lambda _case, _context: correct)
    trace = adapter.read_trace(correct, expected)
    assert trace.fields["bfcl_passed"] is True
    assert trace.fields["tool_call_count"] == 1
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "status": "passed",
                    "benchmark": "BFCL",
                    "phase": "A",
                    "mode": "zero-cost adapter validation",
                    "checks": [
                        "JSONL question/answer contract",
                        "tool-call normalization",
                        "correct call acceptance",
                        "missing call rejection",
                        "wrong parameter rejection",
                        "extra call rejection",
                        "NormalizedTrace conversion",
                    ],
                    "claim_status": "not_claimable",
                    "limitations": [
                        "No model or BFCL dataset cases were executed.",
                        "This is not an official BFCL score.",
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the BFCL-to-framework adapter.")
    parser.add_argument("--mode", choices=("convert", "self-check"), default="self-check")
    parser.add_argument("--dataset", type=Path)
    parser.add_argument("--answers", type=Path)
    parser.add_argument("--category", default="simple_python")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.mode == "self-check":
        self_check(args.output)
        print("BFCL adapter phase-A self-check passed")
        return 0
    if not args.dataset or not args.answers or not args.output:
        parser.error("convert requires --dataset, --answers, and --output")
    cases = convert_cases(
        load_jsonl(args.dataset),
        load_jsonl(args.answers),
        args.category,
        args.limit,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    print(f"converted {len(cases)} BFCL cases to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
