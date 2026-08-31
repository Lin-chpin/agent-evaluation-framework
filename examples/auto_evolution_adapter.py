from __future__ import annotations

from pathlib import Path

from agent_eval import (
    AutoEvolutionAdapter,
    EvalCase,
    EvolutionDiagnosis,
    NormalizedTrace,
    ProjectAdapter,
    Rule,
    RunContext,
    TextCandidate,
    TraceEvent,
)


ROOT = Path(__file__).parent


def build_adapter(artifact: Path, version: str) -> ProjectAdapter:
    keywords = {
        value.strip().lower()
        for value in artifact.read_text(encoding="utf-8").partition("=")[2].split(",")
        if value.strip()
    }

    def call_agent(case: EvalCase, context: RunContext) -> dict:
        message = case.payload["message"].lower()
        route = "SUPPORT" if any(keyword in message for keyword in keywords) else "GENERAL"
        return {
            "trace_id": f"{context.run_id}-{case.case_id}",
            "route": route,
            "quality": 0.9 if route == case.expected["route"] else 0.2,
        }

    def read_trace(handle: dict, _: EvalCase) -> NormalizedTrace:
        return NormalizedTrace(
            trace_id=handle["trace_id"],
            final_output={"route": handle["route"]},
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


def diagnose(summary: dict) -> EvolutionDiagnosis:
    failed_ids = tuple(
        result["case"]["case_id"] for result in summary["results"] if not result["hard_pass"]
    )
    return EvolutionDiagnosis(
        summary="The support routing skill misses password-reset requests.",
        target_type="skill",
        target_id="intent-router",
        evidence_case_ids=failed_ids,
        suspected_modules=("IntentRouter",),
        constraints=("Preserve existing support keywords.",),
    )


def generate_candidates(
    _: EvolutionDiagnosis, current_content: str, round_number: int
) -> tuple[TextCandidate, ...]:
    if round_number > 1:
        return ()
    return (
        TextCandidate(
            "replace-keywords",
            "2-bad",
            "support_keywords=password\n",
            "Replace the old keyword with the failed-case keyword.",
        ),
        TextCandidate(
            "preserve-and-extend",
            "2",
            current_content.rstrip() + ",password\n",
            "Preserve the baseline behavior and add password routing.",
        ),
    )


AUTO_EVOLUTION = AutoEvolutionAdapter(
    name="intent-router-auto-evolution",
    baseline_artifact=ROOT / "auto_evolution.skill.txt",
    baseline_version="1",
    target_type="skill",
    target_id="intent-router",
    diagnose=diagnose,
    generate_candidates=generate_candidates,
    build_adapter=build_adapter,
)
