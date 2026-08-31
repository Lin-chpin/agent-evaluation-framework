from __future__ import annotations

from agent_eval import EvalCase, NormalizedTrace, ProjectAdapter, Rule, RunContext, TraceEvent


def call_agent(case: EvalCase, context: RunContext) -> dict:
    # Replace this body with HTTP, SDK, subprocess, or an in-process Agent call.
    message = case.payload["message"]
    return {
        "trace_id": f"{context.run_id}-{case.case_id}",
        "output": message,
        "route": "ECHO",
        "events": [
            {"module": "EchoAgent", "action": "reply", "duration_ms": 1},
        ],
    }


def read_trace(handle: dict, case: EvalCase) -> NormalizedTrace:
    # Convert the project's native Trace into the framework's stable contract.
    return NormalizedTrace(
        trace_id=handle["trace_id"],
        final_output=handle["output"],
        events=tuple(TraceEvent(**event) for event in handle["events"]),
        fields={"route": handle["route"]},
        target_type="skill",
        target_id="echo",
        target_version="1",
    )


ADAPTER = ProjectAdapter(
    name="replace-me",
    call_agent=call_agent,
    read_trace=read_trace,
    hard_gates=(
        Rule(
            name="route",
            actual="fields.route",
            expected="route",
            suspected_modules=("Router",),
        ),
    ),
    soft_quality=(
        Rule(
            name="expected_keyword",
            actual="final_output",
            operator="contains",
            expected="keyword",
            suspected_modules=("ResponseGenerator",),
        ),
    ),
)

