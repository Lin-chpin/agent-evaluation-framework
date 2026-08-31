from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .engine import EvaluationEngine
from .model import (
    EvalCase,
    EvolutionCandidate,
    EvolutionPolicy,
    MetricObjective,
    to_jsonable,
)
from .rules import read_path


def load_candidate(path: Path) -> EvolutionCandidate:
    return EvolutionCandidate.from_dict(json.loads(path.read_text(encoding="utf-8")))


def load_policy(path: Path | None) -> EvolutionPolicy:
    if path is None:
        return EvolutionPolicy()
    return EvolutionPolicy.from_dict(json.loads(path.read_text(encoding="utf-8")))


def _aggregate(values: list[float], aggregation: str) -> float | None:
    if not values:
        return None
    if aggregation == "sum":
        return sum(values)
    if aggregation == "min":
        return min(values)
    if aggregation == "max":
        return max(values)
    return sum(values) / len(values)


def _objective_value(summary: Mapping[str, Any], objective: MetricObjective) -> float | None:
    values: list[float] = []
    for result in summary["results"]:
        trace = result.get("trace") or {}
        events = trace.get("events", [])
        builtins = {
            "hard_pass": float(bool(result.get("hard_pass"))),
            "soft_warning_count": float(result.get("soft_warning_count", 0)),
            "latency_ms": sum(float(event.get("duration_ms", 0)) for event in events),
            "steps": float(len(events)),
        }
        value = builtins.get(objective.metric)
        if value is None:
            value = read_path(result, objective.metric)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values.append(float(value))
    return _aggregate(values, objective.aggregation)


def summarize_metrics(
    summary: Mapping[str, Any], objectives: Sequence[MetricObjective]
) -> dict[str, Any]:
    results = summary["results"]
    case_count = len(results)
    latencies: list[float] = []
    steps: list[float] = []
    for result in results:
        events = (result.get("trace") or {}).get("events", [])
        latencies.append(sum(float(event.get("duration_ms", 0)) for event in events))
        steps.append(float(len(events)))
    return {
        "case_count": case_count,
        "hard_failures": summary["hard_failures"],
        "hard_pass_rate": (case_count - summary["hard_failures"]) / case_count
        if case_count
        else 0,
        "soft_warning_count": summary["soft_warnings"],
        "mean_latency_ms": _aggregate(latencies, "mean") or 0,
        "mean_steps": _aggregate(steps, "mean") or 0,
        "objectives": {
            objective.name: _objective_value(summary, objective) for objective in objectives
        },
    }


def summarize_scenarios(summary: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    scenarios: dict[str, dict[str, Any]] = {}
    for result in summary["results"]:
        scenario = str(result["case"].get("scenario", "default"))
        metrics = scenarios.setdefault(
            scenario,
            {"case_count": 0, "hard_failures": 0, "soft_warning_count": 0},
        )
        metrics["case_count"] += 1
        metrics["hard_failures"] += int(not result.get("hard_pass"))
        metrics["soft_warning_count"] += int(result.get("soft_warning_count", 0))
    for metrics in scenarios.values():
        metrics["hard_pass_rate"] = (
            metrics["case_count"] - metrics["hard_failures"]
        ) / metrics["case_count"]
    return scenarios


def compare_scenarios(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    baseline_scenarios = summarize_scenarios(baseline)
    candidate_scenarios = summarize_scenarios(candidate)
    comparisons: dict[str, dict[str, Any]] = {}
    for scenario in sorted(set(baseline_scenarios) | set(candidate_scenarios)):
        baseline_metrics = baseline_scenarios.get(scenario)
        candidate_metrics = candidate_scenarios.get(scenario)
        comparison: dict[str, Any] = {
            "baseline": baseline_metrics,
            "candidate": candidate_metrics,
        }
        if baseline_metrics is not None and candidate_metrics is not None:
            comparison["hard_pass_rate_delta"] = (
                candidate_metrics["hard_pass_rate"]
                - baseline_metrics["hard_pass_rate"]
            )
            comparison["soft_warning_delta"] = (
                candidate_metrics["soft_warning_count"]
                - baseline_metrics["soft_warning_count"]
            )
        comparisons[scenario] = comparison
    return comparisons


def _improvement(baseline: float, candidate: float, direction: str) -> float:
    return candidate - baseline if direction == "maximize" else baseline - candidate


def _matches_target(summary: Mapping[str, Any], candidate: EvolutionCandidate, version: str) -> bool:
    traces = [result.get("trace") for result in summary["results"] if result.get("trace")]
    return bool(traces) and all(
        trace.get("target_type") == candidate.target_type
        and trace.get("target_id") == candidate.target_id
        and trace.get("target_version") == version
        for trace in traces
    )


class EvolutionEngine:
    def __init__(
        self,
        baseline: EvaluationEngine,
        candidate: EvaluationEngine,
        policy: EvolutionPolicy | None = None,
    ):
        self.baseline = baseline
        self.candidate = candidate
        self.policy = policy or EvolutionPolicy()

    def run(
        self,
        change: EvolutionCandidate,
        datasets: Mapping[str, Sequence[EvalCase]],
        source: str = "online",
        experiment_id: str | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        required_roles = {"improvement", "regression", "holdout"}
        missing = required_roles.difference(datasets)
        if missing:
            raise ValueError(f"evolution datasets require: {', '.join(sorted(missing))}")
        if any(not datasets[role] for role in required_roles):
            raise ValueError("evolution datasets must not be empty")

        experiment_id = experiment_id or f"evolution-{uuid.uuid4().hex[:12]}"
        baseline_runs: dict[str, Any] = {}
        candidate_runs: dict[str, Any] = {}
        comparisons: dict[str, Any] = {}

        for role in ("improvement", "regression", "holdout"):
            cases = datasets[role]
            baseline_summary = self.baseline.run_suite(
                cases,
                role,
                source=source,
                run_id=f"{experiment_id}-{role}-baseline",
                resume=resume,
            )
            candidate_summary = self.candidate.run_suite(
                cases,
                role,
                source=source,
                run_id=f"{experiment_id}-{role}-candidate",
                resume=resume,
            )
            baseline_metrics = summarize_metrics(baseline_summary, self.policy.objectives)
            candidate_metrics = summarize_metrics(candidate_summary, self.policy.objectives)
            baseline_runs[role] = baseline_summary
            candidate_runs[role] = candidate_summary
            comparisons[role] = {
                "baseline": baseline_metrics,
                "candidate": candidate_metrics,
                "hard_pass_rate_delta": (
                    candidate_metrics["hard_pass_rate"] - baseline_metrics["hard_pass_rate"]
                ),
                "soft_warning_delta": (
                    candidate_metrics["soft_warning_count"]
                    - baseline_metrics["soft_warning_count"]
                ),
                "mean_latency_ms_delta": (
                    candidate_metrics["mean_latency_ms"]
                    - baseline_metrics["mean_latency_ms"]
                ),
                "mean_steps_delta": (
                    candidate_metrics["mean_steps"] - baseline_metrics["mean_steps"]
                ),
                "scenarios": compare_scenarios(baseline_summary, candidate_summary),
            }

        decision, reasons = self._decide(change, baseline_runs, candidate_runs, comparisons)
        result = {
            "experiment_id": experiment_id,
            "candidate": to_jsonable(change),
            "policy": to_jsonable(self.policy),
            "decision": decision,
            "reasons": reasons,
            "comparisons": comparisons,
            "baseline_runs": baseline_runs,
            "candidate_runs": candidate_runs,
        }
        self.baseline.store.save_evolution(experiment_id, change.candidate_id, decision, result)
        return result

    def _decide(
        self,
        change: EvolutionCandidate,
        baseline_runs: Mapping[str, Mapping[str, Any]],
        candidate_runs: Mapping[str, Mapping[str, Any]],
        comparisons: Mapping[str, Mapping[str, Any]],
    ) -> tuple[str, list[str]]:
        rollback: list[str] = []
        reject: list[str] = []
        improvement_evidence: list[str] = []

        for role in ("improvement", "regression", "holdout"):
            if not _matches_target(
                baseline_runs[role], change, change.baseline_version
            ) or not _matches_target(
                candidate_runs[role], change, change.candidate_version
            ):
                rollback.append(f"{role}: target identity or version does not match candidate manifest")

        regression = comparisons["regression"]
        if self.policy.require_regression_pass and regression["candidate"]["hard_failures"]:
            rollback.append("regression: candidate has hard failures")
        if regression["hard_pass_rate_delta"] < 0:
            rollback.append("regression: hard pass rate decreased")
        if (
            regression["soft_warning_delta"]
            > self.policy.max_regression_soft_warning_increase
        ):
            rollback.append("regression: soft warnings exceeded the allowed increase")

        holdout = comparisons["holdout"]
        if self.policy.require_holdout_pass and holdout["candidate"]["hard_failures"]:
            rollback.append("holdout: candidate has hard failures")
        if holdout["hard_pass_rate_delta"] < -self.policy.max_holdout_hard_pass_drop:
            rollback.append("holdout: hard pass rate exceeded the allowed drop")
        if holdout["soft_warning_delta"] > self.policy.max_holdout_soft_warning_increase:
            rollback.append("holdout: soft warnings exceeded the allowed increase")

        for gate in self.policy.scenario_gates:
            for role in gate.roles:
                scenario = comparisons[role]["scenarios"].get(gate.scenario)
                if scenario is None or scenario.get("candidate") is None:
                    rollback.append(f"{role}/{gate.scenario}: configured scenario is missing")
                    continue
                candidate_metrics = scenario["candidate"]
                if candidate_metrics["case_count"] < gate.minimum_case_count:
                    rollback.append(
                        f"{role}/{gate.scenario}: scenario has fewer than "
                        f"{gate.minimum_case_count} cases"
                    )
                if (
                    gate.minimum_hard_pass_rate is not None
                    and candidate_metrics["hard_pass_rate"] < gate.minimum_hard_pass_rate
                ):
                    rollback.append(
                        f"{role}/{gate.scenario}: hard pass rate is below the configured minimum"
                    )
                if (
                    scenario.get("hard_pass_rate_delta", 0)
                    < -gate.maximum_hard_pass_rate_drop
                ):
                    rollback.append(
                        f"{role}/{gate.scenario}: hard pass rate exceeded the allowed drop"
                    )
                if (
                    scenario.get("soft_warning_delta", 0)
                    > gate.maximum_soft_warning_increase
                ):
                    rollback.append(
                        f"{role}/{gate.scenario}: soft warnings exceeded the allowed increase"
                    )

        for objective in self.policy.objectives:
            for role in ("improvement", "regression", "holdout"):
                baseline_value = comparisons[role]["baseline"]["objectives"][objective.name]
                candidate_value = comparisons[role]["candidate"]["objectives"][objective.name]
                if baseline_value is None or candidate_value is None:
                    if objective.required:
                        rollback.append(f"{role}/{objective.name}: required metric is missing")
                    continue
                delta = _improvement(baseline_value, candidate_value, objective.direction)
                if delta < -objective.maximum_regression:
                    rollback.append(f"{role}/{objective.name}: exceeded maximum regression")
                if role == "improvement":
                    if delta < objective.minimum_improvement:
                        reject.append(f"improvement/{objective.name}: minimum improvement not met")
                    elif delta > 0:
                        improvement_evidence.append(f"objective improved: {objective.name}")

        improvement = comparisons["improvement"]
        if improvement["hard_pass_rate_delta"] > 0:
            improvement_evidence.append("improvement: hard pass rate increased")
        if improvement["soft_warning_delta"] < 0:
            improvement_evidence.append("improvement: soft warnings decreased")

        if rollback:
            return "rollback", rollback
        if reject:
            return "reject", reject
        if self.policy.require_improvement and not improvement_evidence:
            return "reject", ["candidate produced no measurable improvement"]
        return "accept", improvement_evidence or ["candidate satisfied all configured gates"]
