from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT.parent.parent))

from agent_eval import (
    AutoEvolutionAdapter,
    EvolutionDiagnosis,
    OpenAICompatibleTextEvolver,
    ProjectAdapter,
)
from agent_eval.llm import OpenAICompatibleReviewer
from integrations.ai_health_assistant.adapter import ADAPTER as TRACE_RULES
from integrations.ai_health_assistant.adapter import read_trace as normalize_trace
from integrations.ai_health_assistant.isolated_runtime import IsolatedHealthRuntime


_RUNTIME = IsolatedHealthRuntime()
_EVOLVER: OpenAICompatibleTextEvolver | None = None


def _evolver() -> OpenAICompatibleTextEvolver:
    global _EVOLVER
    if _EVOLVER is None:
        api_key = os.getenv("AGENT_EVAL_API_KEY") or os.getenv("LLM_API_KEY", "")
        if not api_key:
            raise ValueError("prompt evolution requires AGENT_EVAL_API_KEY or LLM_API_KEY")
        client = OpenAICompatibleReviewer(
            base_url=os.getenv("AGENT_EVAL_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/"),
            api_key=api_key,
            model=os.getenv("AGENT_EVAL_MODEL", "Qwen/Qwen3-14B"),
            timeout_seconds=float(os.getenv("AGENT_EVAL_LLM_TIMEOUT", "120")),
        )
        _EVOLVER = OpenAICompatibleTextEvolver(
            client,
            target_type="prompt",
            target_id="PLANNER",
            change_type="prompt",
            max_candidates=2,
        )
    return _EVOLVER


def build_adapter(artifact: Path, version: str) -> ProjectAdapter:
    prompt = artifact.read_text(encoding="utf-8")

    def call_agent(case, _context):
        traces = _RUNTIME.evaluate(prompt)
        if case.case_id not in traces:
            raise KeyError(f"isolated ai-health returned no trace for {case.case_id}")
        return {"trace": traces[case.case_id]}

    def read_trace(handle, case):
        trace = normalize_trace(handle, case)
        return replace(
            trace,
            target_type="prompt",
            target_id="PLANNER",
            target_version=version,
        )

    return ProjectAdapter(
        name=f"ai-health-planner-{version}",
        call_agent=call_agent,
        read_trace=read_trace,
        hard_gates=TRACE_RULES.hard_gates,
        soft_quality=TRACE_RULES.soft_quality,
    )


def diagnose(summary: dict) -> EvolutionDiagnosis:
    diagnosis = _evolver().diagnose(summary)
    return replace(
        diagnosis,
        constraints=diagnosis.constraints
        + (
            "Modify only the PLANNER prompt.",
            "Preserve the {user_input} placeholder and every required output field.",
            "Do not weaken RISK interception, medical boundaries, or follow-up safety.",
            "Return the complete prompt, not a patch or explanation.",
        ),
    )


def generate_candidates(diagnosis, current_content, round_number):
    return _evolver().generate_candidates(diagnosis, current_content, round_number)


AUTO_EVOLUTION = AutoEvolutionAdapter(
    name="ai-health-planner-auto-evolution",
    baseline_artifact=ROOT / "benchmarks" / "planner_route_defect.prompt.txt",
    baseline_version="defect-v1",
    target_type="prompt",
    target_id="PLANNER",
    diagnose=diagnose,
    generate_candidates=generate_candidates,
    build_adapter=build_adapter,
)
