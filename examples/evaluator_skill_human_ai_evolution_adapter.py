from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

from agent_eval import (
    AutoEvolutionAdapter,
    EvolutionDiagnosis,
    NormalizedTrace,
    OpenAICompatibleTextEvolver,
    ProjectAdapter,
    Rule,
    TraceEvent,
)
from agent_eval.llm import OpenAICompatibleReviewer


ROOT = Path(__file__).parent
_CLIENT: OpenAICompatibleReviewer | None = None
_EVOLVER: OpenAICompatibleTextEvolver | None = None


def _client() -> OpenAICompatibleReviewer:
    global _CLIENT
    if _CLIENT is None:
        api_key = os.getenv("AGENT_EVAL_API_KEY") or os.getenv("LLM_API_KEY", "")
        if not api_key:
            raise ValueError("AI evaluator evolution requires AGENT_EVAL_API_KEY or LLM_API_KEY")
        _CLIENT = OpenAICompatibleReviewer(
            base_url=os.getenv("AGENT_EVAL_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/"),
            api_key=api_key,
            model=os.getenv("AGENT_EVAL_MODEL", "Qwen/Qwen3-14B"),
            timeout_seconds=float(os.getenv("AGENT_EVAL_LLM_TIMEOUT", "120")),
            temperature=float(os.getenv("AGENT_EVAL_TEMPERATURE", "0")),
            provider=os.getenv("AGENT_EVAL_PROVIDER", "siliconflow"),
        )
    return _CLIENT


def _evolver() -> OpenAICompatibleTextEvolver:
    global _EVOLVER
    if _EVOLVER is None:
        _EVOLVER = OpenAICompatibleTextEvolver(
            _client(),
            target_type="evaluator_skill",
            target_id="human-reviewed-ai-report-reviewer",
            change_type="skill",
            max_candidates=1,
        )
    return _EVOLVER


def build_adapter(artifact: Path, version: str) -> ProjectAdapter:
    skill_prompt = artifact.read_text(encoding="utf-8")

    def call_agent(case, context):
        if skill_prompt.strip() == "MODE=constant-correct":
            result = {"verdict": "CORRECT", "reason": "legacy constant rule"}
            runtime = {
                "runtime_type": "deterministic",
                "provider": None,
                "model": None,
                "temperature": None,
            }
        else:
            client = _client()
            result = client.request_json(
                skill_prompt
                + "\n\nINPUT_DATA:\n"
                + json.dumps(case.payload, ensure_ascii=False)
            )
            runtime = {
                "runtime_type": "llm",
                "provider": client.provider,
                "model": client.model,
                "temperature": client.temperature,
            }
        verdict = str(result.get("verdict", "")).strip().upper()
        if verdict not in {"CORRECT", "INCORRECT"}:
            raise ValueError(f"evaluator Skill returned invalid verdict: {verdict!r}")
        return {
            "trace_id": f"{context.run_id}-{case.case_id}",
            "verdict": verdict,
            "reason": str(result.get("reason", "")),
            "quality": 1.0 if verdict == case.expected["verdict"] else 0.0,
            "runtime": runtime,
        }

    def read_trace(handle, _case):
        return NormalizedTrace(
            trace_id=handle["trace_id"],
            final_output={"verdict": handle["verdict"], "reason": handle["reason"]},
            events=(TraceEvent("AIReportEvaluatorSkill", "judge"),),
            fields={
                "verdict": handle["verdict"],
                "quality": handle["quality"],
                **handle["runtime"],
            },
            target_type="evaluator_skill",
            target_id="human-reviewed-ai-report-reviewer",
            target_version=version,
        )

    return ProjectAdapter(
        name=f"human-reviewed-ai-report-reviewer-{version}",
        call_agent=call_agent,
        read_trace=read_trace,
        hard_gates=(Rule("human_verdict", "fields.verdict", expected="verdict"),),
        soft_quality=(),
        max_concurrency=1,
    )


def _improvement_evidence(summary: dict) -> list[dict]:
    evidence = []
    for result in summary["results"]:
        if result["hard_pass"] and not result["soft_warning_count"]:
            continue
        case = result["case"]
        metadata = case.get("metadata", {})
        if metadata.get("dataset_role") != "improvement":
            raise ValueError("candidate evidence must contain improvement cases only")
        evidence.append(
            {
                "case_id": case["case_id"],
                "facts": case["payload"]["facts"],
                "agent_output": case["payload"]["agent_output"],
                "evaluation_report": case["payload"]["evaluation_report"],
                "human_review_outcome": metadata["human_review_outcome"],
                "gate_verdict": metadata["gate_verdict"],
                "gold_correction": metadata.get("gold_correction"),
            }
        )
    return evidence


def diagnose(summary: dict) -> EvolutionDiagnosis:
    diagnosis = _evolver().diagnose(summary)
    metadata = dict(diagnosis.metadata)
    metadata.update(
        {
            "improvement_evidence": _improvement_evidence(summary),
            "candidate_evidence_scope": {
                "included": ["improvement"],
                "excluded": ["regression", "holdout"],
            },
        }
    )
    return replace(
        diagnosis,
        constraints=diagnosis.constraints
        + (
            "Use only the supplied facts, Agent output, and evaluation report.",
            "A partially correct evaluation report must be classified as INCORRECT.",
            "Check every decisive condition, arithmetic relationship, and claim strength.",
            "Return the complete evaluator Skill prompt, not a patch or explanation.",
            "Do not weaken behavior already confirmed by human review.",
        ),
        metadata=metadata,
    )


def generate_candidates(diagnosis, current_content, round_number):
    return _evolver().generate_candidates(diagnosis, current_content, round_number)


AUTO_EVOLUTION = AutoEvolutionAdapter(
    name="human-reviewed-ai-evaluator-skill-auto-evolution",
    baseline_artifact=ROOT / "evaluator_skill_human_ai.skill.txt",
    baseline_version="ai-defect-v1",
    target_type="evaluator_skill",
    target_id="human-reviewed-ai-report-reviewer",
    diagnose=diagnose,
    generate_candidates=generate_candidates,
    build_adapter=build_adapter,
)
