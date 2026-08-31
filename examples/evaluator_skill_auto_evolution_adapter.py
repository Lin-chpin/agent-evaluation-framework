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
    signals = {
        value.strip().lower()
        for value in artifact.read_text(encoding="utf-8").partition("=")[2].split(",")
        if value.strip()
    }

    def call_agent(case: EvalCase, context: RunContext) -> dict[str, str | float]:
        report = case.payload["evaluation_report"].lower()
        verdict = "INCORRECT" if any(signal in report for signal in signals) else "CORRECT"
        return {
            "trace_id": f"{context.run_id}-{case.case_id}",
            "verdict": verdict,
            "quality": 0.9 if verdict == case.expected["verdict"] else 0.2,
        }

    def read_trace(handle: dict[str, str | float], _: EvalCase) -> NormalizedTrace:
        return NormalizedTrace(
            trace_id=str(handle["trace_id"]),
            final_output={"verdict": handle["verdict"]},
            events=(TraceEvent("ReportEvaluatorSkill", "judge", duration_ms=2),),
            fields={"verdict": handle["verdict"], "quality": handle["quality"]},
            target_type="evaluator_skill",
            target_id="report-reviewer",
            target_version=version,
        )

    return ProjectAdapter(
        name=f"report-reviewer-{version}",
        call_agent=call_agent,
        read_trace=read_trace,
        hard_gates=(Rule("human_verdict", "fields.verdict", expected="verdict"),),
        soft_quality=(),
    )


def diagnose(summary: dict) -> EvolutionDiagnosis:
    failed_ids = tuple(
        result["case"]["case_id"]
        for result in summary["results"]
        if not result["hard_pass"]
    )
    return EvolutionDiagnosis(
        summary="The evaluator Skill misses reports containing unsupported claims.",
        target_type="evaluator_skill",
        target_id="report-reviewer",
        evidence_case_ids=failed_ids,
        suspected_modules=("ReportEvaluatorSkill",),
        constraints=("Preserve judgments already confirmed by human review.",),
    )


def generate_candidates(
    _: EvolutionDiagnosis, current_content: str, round_number: int
) -> tuple[TextCandidate, ...]:
    if round_number > 1:
        return ()
    return (
        TextCandidate(
            "replace-review-rule",
            "2-bad",
            "error_signals=unsupported\n",
            "Replace the existing reviewed-error signal with the new one.",
            change_type="skill",
        ),
        TextCandidate(
            "preserve-and-extend-review-rule",
            "2",
            current_content.rstrip() + ",unsupported\n",
            "Preserve reviewed behavior and generalize the new error pattern.",
            change_type="skill",
        ),
    )


AUTO_EVOLUTION = AutoEvolutionAdapter(
    name="evaluator-skill-auto-evolution",
    baseline_artifact=ROOT / "evaluator_skill.skill.txt",
    baseline_version="1",
    target_type="evaluator_skill",
    target_id="report-reviewer",
    diagnose=diagnose,
    generate_candidates=generate_candidates,
    build_adapter=build_adapter,
)
