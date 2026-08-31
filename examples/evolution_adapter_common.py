from __future__ import annotations

from agent_eval import EvalCase, NormalizedTrace, ProjectAdapter, Rule, RunContext, TraceEvent


def build_adapter(version: str, support_routing: bool) -> ProjectAdapter:
    def call_agent(case: EvalCase, context: RunContext) -> dict:
        message = case.payload["message"].lower()
        is_support = any(term in message for term in ("support", "password", "billing"))
        route = "SUPPORT" if support_routing and is_support else "GENERAL"
        quality = 0.9 if route == case.expected["route"] else 0.2
        return {
            "trace_id": f"{context.run_id}-{case.case_id}",
            "output": {"route": route},
            "route": route,
            "quality": quality,
        }

    def read_trace(handle: dict, _: EvalCase) -> NormalizedTrace:
        return NormalizedTrace(
            trace_id=handle["trace_id"],
            final_output=handle["output"],
            events=(TraceEvent("IntentRouter", "route", duration_ms=2),),
            fields={"route": handle["route"], "quality": handle["quality"]},
            target_type="skill",
            target_id="intent-router",
            target_version=version,
        )

    return ProjectAdapter(
        name=f"intent-router-{version}",
        call_agent=call_agent,
        read_trace=read_trace,
        hard_gates=(Rule("route", "fields.route", expected="route"),),
        soft_quality=(),
    )
