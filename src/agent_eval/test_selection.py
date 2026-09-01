from __future__ import annotations

import ipaddress
import json
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

from .diff_analysis import DiffSnapshot, assess_rules, read_git_diff
from .model import TestImpactAssessment, TestSelection


class JsonReviewer(Protocol):
    base_url: str

    def request_json(self, prompt: str) -> Mapping[str, Any]: ...


_MODE_RANK = {"smoke": 0, "regression": 1, "full": 2}
_SUITES = {
    "smoke": ("smoke",),
    "regression": ("regression", "smoke"),
    "full": ("regression", "smoke", "full"),
}


def _is_private_endpoint(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _ai_assessment(reviewer: JsonReviewer, snapshot: DiffSnapshot, input_kind: str) -> TestImpactAssessment:
    evidence = snapshot.raw_diff if input_kind == "raw" else snapshot.sanitized_summary()
    prompt = (
        "Assess the real test impact of this Git change. Return one JSON object with mode "
        "(smoke, regression, or full), confidence (0..1), risk (low, medium, or high), "
        "and reasons (an array of short strings). Smoke is for UI/report-only low-risk changes; "
        "full is for generation, RAG/retrieval, or evaluation-result schema changes; regression "
        "is for Planner, Prompt, safety, follow-up, quality logic, and other protected behavior.\n\n"
        + evidence
    )
    value = reviewer.request_json(prompt)
    return TestImpactAssessment(
        mode=str(value.get("mode", "")).lower(),
        confidence=float(value.get("confidence", 0)),
        risk=str(value.get("risk", "medium")).lower(),
        reasons=tuple(str(reason) for reason in value.get("reasons", ())),
        source="ai",
        input_kind=input_kind,
    )


def select_tests(
    repository: Path | str = ".",
    base: str = "HEAD",
    *,
    ai_provider: str = "none",
    ai_input: str = "auto",
    reviewer: JsonReviewer | None = None,
    confidence_threshold: float = 0.7,
) -> TestSelection:
    if ai_provider not in {"none", "local", "remote"}:
        raise ValueError("ai_provider must be none, local, or remote")
    if ai_input not in {"auto", "raw", "summary"}:
        raise ValueError("ai_input must be auto, raw, or summary")
    if not 0 <= confidence_threshold <= 1:
        raise ValueError("confidence_threshold must be between 0 and 1")

    snapshot = read_git_diff(repository, base)
    rules = assess_rules(snapshot)
    ai = None
    review_reasons: list[str] = []

    if ai_provider != "none":
        if reviewer is None:
            raise ValueError("AI selection requires a reviewer")
        if ai_provider == "local" and not _is_private_endpoint(reviewer.base_url):
            raise ValueError("local AI requires a loopback, private-IP, or .local endpoint")
        if ai_provider == "remote" and ai_input == "raw":
            raise ValueError("remote AI can only receive a sanitized summary")
        input_kind = (
            "raw" if ai_input == "auto" and ai_provider == "local" else
            "summary" if ai_input == "auto" else ai_input
        )
        try:
            ai = _ai_assessment(reviewer, snapshot, input_kind)
        except Exception as error:
            review_reasons.append(f"AI assessment failed: {type(error).__name__}")

    final_mode = rules.mode
    if ai is not None:
        if ai.confidence < confidence_threshold:
            review_reasons.append("AI confidence is below the configured threshold.")
        if ai.mode != rules.mode:
            review_reasons.append("AI and deterministic rules recommend different modes.")
        if _MODE_RANK[ai.mode] > _MODE_RANK[final_mode]:
            final_mode = ai.mode
    if rules.risk == "high" or (ai is not None and ai.risk == "high"):
        review_reasons.append("The change is high risk.")

    return TestSelection(
        mode=final_mode,
        suites=_SUITES[final_mode],
        rule_assessment=rules,
        ai_assessment=ai,
        human_review_required=bool(review_reasons),
        review_reasons=tuple(dict.fromkeys(review_reasons)),
        changed_files=len(snapshot.files),
        additions=snapshot.additions,
        deletions=snapshot.deletions,
    )
