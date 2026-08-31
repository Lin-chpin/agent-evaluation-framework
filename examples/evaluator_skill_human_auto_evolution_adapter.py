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


def _judge(policy: str, case: EvalCase) -> str:
    report = str(case.payload["evaluation_report"])
    facts = str(case.payload["facts"])

    if policy == "reject_pass_reports":
        return "INCORRECT" if report.startswith("通过") else "CORRECT"
    if policy == "validate_decisive_claims":
        arithmetic_error = "剩余应付金额为500元" in facts and "已支付金额" in report
        missing_required_condition = "风险评分为55" in facts and report.startswith("通过")
        unsupported_capability = "不支持PDF导出" in facts and "整体可接受" in report
        unsupported_stability = "没有提供置信区间或显著性检验" in facts and "稳定" in report
        if (
            arithmetic_error
            or missing_required_condition
            or unsupported_capability
            or unsupported_stability
        ):
            return "INCORRECT"
    return "CORRECT"


def build_adapter(artifact: Path, version: str) -> ProjectAdapter:
    policy = artifact.read_text(encoding="utf-8").partition("=")[2].strip()

    def call_agent(case: EvalCase, context: RunContext) -> dict[str, str | float]:
        verdict = _judge(policy, case)
        return {
            "trace_id": f"{context.run_id}-{case.case_id}",
            "verdict": verdict,
            "quality": 1.0 if verdict == case.expected["verdict"] else 0.0,
        }

    def read_trace(handle: dict[str, str | float], _: EvalCase) -> NormalizedTrace:
        return NormalizedTrace(
            trace_id=str(handle["trace_id"]),
            final_output={"verdict": handle["verdict"]},
            events=(TraceEvent("HumanReviewedReportEvaluator", "judge", duration_ms=2),),
            fields={"verdict": handle["verdict"], "quality": handle["quality"]},
            target_type="evaluator_skill",
            target_id="human-reviewed-report-reviewer",
            target_version=version,
        )

    return ProjectAdapter(
        name=f"human-reviewed-report-reviewer-{version}",
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
        summary="The evaluator accepts reports that validate only part of the decisive evidence.",
        target_type="evaluator_skill",
        target_id="human-reviewed-report-reviewer",
        evidence_case_ids=failed_ids,
        suspected_modules=("HumanReviewedReportEvaluator",),
        constraints=("Preserve reports already confirmed as correct by human review.",),
    )


def generate_candidates(
    _: EvolutionDiagnosis, current_content: str, round_number: int
) -> tuple[TextCandidate, ...]:
    if round_number > 1:
        return ()
    return (
        TextCandidate(
            "reject-all-pass-reports",
            "2-bad",
            "review_policy=reject_pass_reports\n",
            "Reject every passing report; expected to damage confirmed-correct cases.",
            change_type="skill",
        ),
        TextCandidate(
            "validate-decisive-claims",
            "2",
            "review_policy=validate_decisive_claims\n",
            "Check arithmetic, required conditions, and evidentiary support before accepting.",
            change_type="skill",
        ),
    )


AUTO_EVOLUTION = AutoEvolutionAdapter(
    name="human-reviewed-evaluator-skill-auto-evolution",
    baseline_artifact=ROOT / "evaluator_skill_human.skill.txt",
    baseline_version="1",
    target_type="evaluator_skill",
    target_id="human-reviewed-report-reviewer",
    diagnose=diagnose,
    generate_candidates=generate_candidates,
    build_adapter=build_adapter,
)
