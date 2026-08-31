from __future__ import annotations

from collections import Counter
from typing import Any, Iterable, Mapping, Sequence

from .model import CheckResult, EvalCase, NormalizedTrace, Rule


_MISSING = object()


def read_path(value: Any, path: str) -> Any:
    current = value
    for part in filter(None, path.split(".")):
        if isinstance(current, Mapping):
            current = current.get(part, _MISSING)
        elif isinstance(current, Sequence) and not isinstance(current, (str, bytes)) and part.isdigit():
            index = int(part)
            current = current[index] if index < len(current) else _MISSING
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def _matches(operator: str, actual: Any, expected: Any) -> bool:
    if operator == "exists":
        return actual is not _MISSING and actual is not None
    if actual is _MISSING:
        return False
    try:
        if operator == "eq":
            return actual == expected
        if operator == "ne":
            return actual != expected
        if operator == "contains":
            return expected in actual
        if operator == "not_contains":
            return expected not in actual
        if operator == "max":
            return actual <= expected
        if operator == "min":
            return actual >= expected
        if operator == "in":
            return actual in expected
    except TypeError:
        return False
    raise ValueError(f"unsupported rule operator: {operator}")


def evaluate_rule(
    rule: Rule,
    level: str,
    case: EvalCase,
    trace: NormalizedTrace,
) -> CheckResult:
    trace_data = trace.as_mapping()
    actual = read_path(trace_data, rule.actual)
    expected = read_path(case.expected, rule.expected) if rule.expected else rule.value
    passed = _matches(rule.operator, actual, expected)
    return CheckResult(
        layer="structure",
        level=level,
        name=rule.name,
        passed=passed,
        actual=None if actual is _MISSING else actual,
        expected=None if expected is _MISSING else expected,
        message=rule.message or ("passed" if passed else f"{rule.actual} failed {rule.operator}"),
        suspected_modules=rule.suspected_modules,
    )


def structure_checks(
    case: EvalCase,
    trace: NormalizedTrace,
    hard_gates: Iterable[Rule],
    soft_quality: Iterable[Rule],
) -> list[CheckResult]:
    checks = [
        CheckResult("structure", "hard", "trace_id", bool(trace.trace_id), trace.trace_id, "non-empty"),
        CheckResult(
            "structure",
            "hard",
            "final_output",
            trace.final_output is not None,
            trace.final_output,
            "not null",
        ),
    ]
    checks.extend(evaluate_rule(rule, "hard", case, trace) for rule in hard_gates)
    checks.extend(evaluate_rule(rule, "soft", case, trace) for rule in soft_quality)
    return checks


def behavior_checks(case: EvalCase, trace: NormalizedTrace) -> list[CheckResult]:
    constraints = case.metadata.get("system_constraints", {})
    events = trace.events
    checks: list[CheckResult] = []

    if "max_steps" in constraints:
        limit = constraints["max_steps"]
        checks.append(CheckResult("behavior", "hard", "max_steps", len(events) <= limit, len(events), limit))

    retries = sum(1 for event in events if "retry" in event.action.lower())
    if "max_retries" in constraints:
        limit = constraints["max_retries"]
        checks.append(CheckResult("behavior", "hard", "max_retries", retries <= limit, retries, limit))

    latency = sum(event.duration_ms for event in events)
    if "max_latency_ms" in constraints:
        limit = constraints["max_latency_ms"]
        checks.append(CheckResult("behavior", "hard", "max_latency_ms", latency <= limit, latency, limit))

    max_repeats = int(constraints.get("max_consecutive_repeats", 3))
    longest = 0
    last: tuple[str, str] | None = None
    current = 0
    repeated: tuple[str, str] | None = None
    for event in events:
        key = (event.module, event.action)
        current = current + 1 if key == last else 1
        if current > longest:
            longest, repeated = current, key
        last = key
    if longest > max_repeats:
        module = repeated[0] if repeated else ""
        checks.append(
            CheckResult(
                "behavior",
                "candidate",
                "repeated_event",
                False,
                longest,
                max_repeats,
                f"repeated module/action: {repeated}",
                (module,) if module else (),
            )
        )

    errors = Counter(event.module for event in events if event.error or event.status == "error")
    for module, count in errors.items():
        checks.append(
            CheckResult(
                "behavior",
                "candidate",
                f"module_error:{module}",
                False,
                count,
                0,
                "trace contains module errors",
                (module,),
            )
        )
    return checks


def consistency_checks(
    traces: Sequence[NormalizedTrace],
    rules: Iterable[Rule],
) -> list[CheckResult]:
    if len(traces) < 2:
        return []
    checks: list[CheckResult] = []
    for rule in rules:
        values = [read_path(trace.as_mapping(), rule.actual) for trace in traces]
        if any(value != values[0] for value in values[1:]):
            checks.append(
                CheckResult(
                    "consistency",
                    "candidate",
                    f"inconsistent:{rule.name}",
                    False,
                    [None if value is _MISSING else value for value in values],
                    "stable values",
                    "shadow runs disagree; human review required",
                    rule.suspected_modules,
                )
            )
    return checks


def feedback_checks(trace: NormalizedTrace) -> list[CheckResult]:
    signals = {
        "explicit_negative": "user explicitly rejected the result",
        "repeated_question": "user repeated a semantically similar question",
        "rephrased": "user immediately rephrased the request",
    }
    return [
        CheckResult("feedback", "candidate", name, False, True, False, message)
        for name, message in signals.items()
        if trace.feedback.get(name) is True
    ]
