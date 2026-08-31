from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Sequence
from urllib.request import Request, urlopen

from .model import (
    CheckResult,
    EvalCase,
    EvolutionDiagnosis,
    NormalizedTrace,
    RetryableEvolverError,
    TextCandidate,
    to_jsonable,
)


def _parse_json_object(content: str) -> Mapping[str, Any]:
    """Accept a JSON object even when a model wraps it in prose or a code fence."""
    try:
        value = json.loads(content)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for start, character in enumerate(content):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(content, start)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("model did not return a JSON object")


@dataclass(frozen=True)
class OpenAICompatibleReviewer:
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 60
    temperature: float = 0
    provider: str = "openai-compatible"
    max_tokens: int | None = None

    @classmethod
    def from_environment(cls) -> "OpenAICompatibleReviewer | None":
        model = os.getenv("AGENT_EVAL_MODEL", "").strip()
        if not model:
            return None
        return cls(
            base_url=os.getenv("AGENT_EVAL_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
            api_key=os.getenv("AGENT_EVAL_API_KEY", ""),
            model=model,
            timeout_seconds=float(os.getenv("AGENT_EVAL_LLM_TIMEOUT", "60")),
            temperature=float(os.getenv("AGENT_EVAL_TEMPERATURE", "0")),
            provider=os.getenv("AGENT_EVAL_PROVIDER", "openai-compatible"),
            max_tokens=(
                int(os.environ["AGENT_EVAL_MAX_TOKENS"])
                if os.getenv("AGENT_EVAL_MAX_TOKENS")
                else None
            ),
        )

    def analyze(
        self,
        case: EvalCase,
        traces: Sequence[NormalizedTrace],
        checks: Sequence[CheckResult],
        collect_few_shot: bool,
    ) -> Mapping[str, Any]:
        evidence = {
            "case": to_jsonable(case),
            "traces": [trace.as_mapping() for trace in traces],
            "failed_checks": [to_jsonable(check) for check in checks if not check.passed],
            "collect_few_shot": collect_few_shot,
        }
        prompt = (
            "You are an evidence-bound Agent evaluation reviewer. "
            "Use only the supplied case, traces, and failed checks. "
            "Return JSON with keys: summary, suspected_modules, suggestions, consistency, "
            "few_shot_candidate. Suggestions are review candidates, never final decisions. "
            "Set few_shot_candidate to null unless collect_few_shot is true and the run is clearly successful.\n\n"
            + json.dumps(evidence, ensure_ascii=False)
        )
        return self.request_json(prompt)

    def request_json(self, prompt: str) -> Mapping[str, Any]:
        result, _ = self.request_json_with_usage(prompt)
        return result

    def request_json_with_usage(
        self, prompt: str
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        request_body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        if self.max_tokens is not None:
            request_body["max_tokens"] = self.max_tokens
        body = json.dumps(request_body).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        try:
            result = _parse_json_object(content)
        except ValueError:
            result = {"summary": content, "parse_error": "model did not return valid JSON"}
        return result, payload.get("usage", {})


@dataclass(frozen=True)
class OpenAICompatibleTextEvolver:
    client: OpenAICompatibleReviewer
    target_type: str
    target_id: str
    change_type: str = "skill"
    max_candidates: int = 2

    @classmethod
    def from_environment(
        cls,
        target_type: str,
        target_id: str,
        change_type: str = "skill",
        max_candidates: int = 2,
    ) -> "OpenAICompatibleTextEvolver | None":
        client = OpenAICompatibleReviewer.from_environment()
        if client is None:
            return None
        return cls(client, target_type, target_id, change_type, max(1, max_candidates))

    def diagnose(self, summary: Mapping[str, Any]) -> EvolutionDiagnosis:
        failed_results = [
            result
            for result in summary["results"]
            if not result["hard_pass"] or result["soft_warning_count"]
        ]
        prompt = (
            "Diagnose the supplied Agent or Skill evaluation failures. Use only the evidence. "
            "Return JSON with summary, evidence_case_ids, suspected_modules, constraints, and metadata. "
            "Do not propose production deployment.\n\n"
            + json.dumps(
                {
                    "target_type": self.target_type,
                    "target_id": self.target_id,
                    "run_id": summary["run_id"],
                    "failed_results": failed_results,
                },
                ensure_ascii=False,
            )
        )
        result = self.client.request_json(prompt)
        if result.get("parse_error"):
            raise RetryableEvolverError("diagnosis model did not return valid JSON")
        return EvolutionDiagnosis(
            summary=str(result.get("summary", "")).strip(),
            target_type=self.target_type,
            target_id=self.target_id,
            evidence_case_ids=tuple(str(value) for value in result.get("evidence_case_ids", [])),
            suspected_modules=tuple(str(value) for value in result.get("suspected_modules", [])),
            constraints=tuple(str(value) for value in result.get("constraints", [])),
            metadata=result.get("metadata", {}),
        )

    def generate_candidates(
        self,
        diagnosis: EvolutionDiagnosis,
        current_content: str,
        round_number: int,
    ) -> tuple[TextCandidate, ...]:
        prompt = (
            "Generate bounded text-artifact candidates from the diagnosis. Preserve existing behavior "
            "unless the evidence requires a change. Return JSON with a candidates array. Each candidate "
            "must contain summary and complete_content. Do not include markdown fences.\n\n"
            + json.dumps(
                {
                    "target_type": self.target_type,
                    "target_id": self.target_id,
                    "change_type": self.change_type,
                    "diagnosis": to_jsonable(diagnosis),
                    "current_content": current_content,
                    "maximum_candidates": self.max_candidates,
                },
                ensure_ascii=False,
            )
        )
        result = self.client.request_json(prompt)
        if result.get("parse_error"):
            raise RetryableEvolverError("candidate model did not return valid JSON")
        candidates: list[TextCandidate] = []
        for index, value in enumerate(result.get("candidates", [])[: self.max_candidates], 1):
            candidates.append(
                TextCandidate(
                    candidate_id=f"ai-round-{round_number}-{index}-{uuid.uuid4().hex[:6]}",
                    candidate_version=f"ai-r{round_number}-c{index}-{uuid.uuid4().hex[:6]}",
                    content=str(value.get("complete_content", "")),
                    summary=str(value.get("summary", "AI-generated text candidate")),
                    change_type=self.change_type,
                    metadata={
                        "generator": "openai-compatible",
                        "provider": getattr(self.client, "provider", "openai-compatible"),
                        "model": getattr(self.client, "model", "unknown"),
                        "temperature": getattr(self.client, "temperature", None),
                        **value.get("metadata", {}),
                    },
                )
            )
        return tuple(candidates)
