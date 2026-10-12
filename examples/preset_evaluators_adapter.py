"""No-key demonstration adapter: replace the mock call with your real Agent."""
from __future__ import annotations

from agent_eval import EvalCase, NormalizedTrace, ProjectAdapter, RunContext, TraceEvent


def call_agent(case: EvalCase, context: RunContext) -> dict:
    return {"trace_id": f"{context.run_id}-{case.case_id}",
            "answer": "Paris is the capital of France.",
            "context": "Paris is the capital of France.",
            "tools": ["search_docs", "summarize"]}


def read_trace(handle: dict, case: EvalCase) -> NormalizedTrace:
    return NormalizedTrace(
        trace_id=handle["trace_id"],
        final_output=handle["answer"],
        events=tuple(TraceEvent("OpenAITool", tool, fields={"span_type": "function"})
                     for tool in handle["tools"]),
        fields={"context": handle["context"]},
    )


ADAPTER = ProjectAdapter("preset-demo", call_agent, read_trace, (), ())
