from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_eval.engine import load_cases
from agent_eval.evaluator_metrics import classification_metrics, repeat_stability
from agent_eval.llm import OpenAICompatibleReviewer
from agent_eval.model import to_jsonable


DATASETS = (
    ("improvement", ROOT / "examples/evaluator_skill_human.improvement.jsonl"),
    ("regression", ROOT / "examples/evaluator_skill_human.regression.jsonl"),
    ("holdout", ROOT / "examples/evaluator_skill_human.holdout.jsonl"),
)
SKILL = ROOT / "evidence/evaluator-skill-stability.skill.txt"
PLAN = ROOT / "evidence/evaluator-skill-stability-plan.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf-8")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the preregistered evaluator stability test")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "evidence/evaluator-skill-stability-results.json",
    )
    args = parser.parse_args()
    plan = json.loads(PLAN.read_text("utf-8"))
    cases = [case for role, path in DATASETS for case in load_cases(path, role)]
    if len(cases) != plan["case_count_per_repetition"]:
        raise ValueError("frozen case count does not match the preregistered plan")

    api_key = os.getenv("AGENT_EVAL_API_KEY") or os.getenv("LLM_API_KEY", "")
    if not api_key:
        raise ValueError("AGENT_EVAL_API_KEY or LLM_API_KEY is required")
    client = OpenAICompatibleReviewer(
        base_url=os.getenv("AGENT_EVAL_BASE_URL", "https://api.siliconflow.cn/v1").rstrip("/"),
        api_key=api_key,
        model=os.getenv("AGENT_EVAL_MODEL", plan["model"]),
        timeout_seconds=float(os.getenv("AGENT_EVAL_LLM_TIMEOUT", "120")),
        temperature=float(plan["temperature"]),
        provider="siliconflow",
        max_tokens=int(plan["max_output_tokens_per_call"]),
    )
    if client.model != plan["model"]:
        raise ValueError(f"configured model {client.model!r} does not match preregistration")

    output = args.output if args.output.is_absolute() else ROOT / args.output
    progress = output.with_suffix(".progress.json")
    state = (
        json.loads(progress.read_text("utf-8"))
        if progress.exists()
        else {
            "experiment_id": plan["experiment_id"],
            "plan_sha256": sha256(PLAN),
            "calls": [],
        }
    )
    if state["plan_sha256"] != sha256(PLAN):
        raise ValueError("progress file does not match the preregistered plan")
    completed = {(call["repetition"], call["case_id"]) for call in state["calls"]}
    skill = SKILL.read_text("utf-8")

    for repetition in range(1, plan["repetitions"] + 1):
        for case in cases:
            key = (repetition, case.case_id)
            if key in completed:
                continue
            call = {
                "repetition": repetition,
                "case_id": case.case_id,
                "expected_verdict": case.expected["verdict"],
            }
            try:
                response, usage = client.request_json_with_usage(
                    skill
                    + "\n\nCASE:\n"
                    + json.dumps(to_jsonable(case.payload), ensure_ascii=False)
                )
                verdict = str(response.get("verdict", "")).upper()
                if verdict not in {"CORRECT", "INCORRECT"}:
                    raise ValueError(f"invalid verdict: {verdict!r}")
                call.update(
                    {
                        "status": "success",
                        "verdict": verdict,
                        "reason": str(response.get("reason", "")),
                        "usage": dict(usage),
                    }
                )
            except Exception as error:
                call.update({"status": "error", "error": str(error)})
            state["calls"].append(call)
            save(progress, state)
            print(f"{len(state['calls'])}/{plan['maximum_scoring_calls']} {case.case_id} {call['status']}", flush=True)

    runs = []
    for repetition in range(1, plan["repetitions"] + 1):
        results = []
        for call in state["calls"]:
            if call["repetition"] != repetition or call["status"] != "success":
                continue
            results.append(
                {
                    "case": {
                        "case_id": call["case_id"],
                        "expected": {"verdict": call["expected_verdict"]},
                    },
                    "trace": {"fields": {"verdict": call["verdict"]}},
                }
            )
        runs.append(results)

    by_case = {}
    for case in cases:
        calls = [call for call in state["calls"] if call["case_id"] == case.case_id]
        verdicts = [call["verdict"] for call in calls if call["status"] == "success"]
        by_case[case.case_id] = {
            "expected_verdict": case.expected["verdict"],
            "successful_calls": len(verdicts),
            "verdicts": verdicts,
            "strictly_stable": len(verdicts) == plan["repetitions"] and len(set(verdicts)) == 1,
        }
    successful = [call for call in state["calls"] if call["status"] == "success"]
    usage_keys = {
        key
        for call in successful
        for key, value in call.get("usage", {}).items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }
    result = {
        "schema_version": 1,
        "experiment_id": plan["experiment_id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "plan": str(PLAN.relative_to(ROOT)),
        "plan_sha256": sha256(PLAN),
        "skill_artifact": str(SKILL.relative_to(ROOT)),
        "skill_sha256": sha256(SKILL),
        "model": client.model,
        "provider": client.provider,
        "temperature": client.temperature,
        "max_tokens": client.max_tokens,
        "automatic_retries": 0,
        "datasets": {
            role: {"path": str(path.relative_to(ROOT)), "sha256": sha256(path)}
            for role, path in DATASETS
        },
        "call_budget": plan["maximum_scoring_calls"],
        "successful_calls": len(successful),
        "failed_calls": len(state["calls"]) - len(successful),
        "availability_rate": len(successful) / plan["maximum_scoring_calls"],
        "provider_usage": {
            key: sum(int(call.get("usage", {}).get(key, 0)) for call in successful)
            for key in sorted(usage_keys)
        },
        "per_repetition_metrics": [classification_metrics(run) for run in runs],
        "repeat_stability": repeat_stability(runs),
        "strict_stable_case_count": sum(item["strictly_stable"] for item in by_case.values()),
        "strict_stability_rate": sum(item["strictly_stable"] for item in by_case.values()) / len(by_case),
        "per_case": by_case,
        "calls": state["calls"],
        "limitations": [
            "The ten cases are human-reviewed synthetic cases, not real-business data.",
            "Repeated decisions for the same case are correlated and are not fifty independent business samples.",
            "This experiment measures one frozen Skill and model configuration; it does not estimate all-model reliability."
        ],
    }
    save(output, result)
    progress.unlink(missing_ok=True)
    print(json.dumps({key: result[key] for key in ("successful_calls", "failed_calls", "strict_stability_rate", "provider_usage")}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
