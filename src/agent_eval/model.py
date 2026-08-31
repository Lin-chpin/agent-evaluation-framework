from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    payload: Any
    expected: Mapping[str, Any] = field(default_factory=dict)
    scenario: str = "default"
    metadata: Mapping[str, Any] = field(default_factory=dict)
    suite: str = "custom"

    @classmethod
    def from_dict(cls, value: Mapping[str, Any], suite: str) -> "EvalCase":
        case_id = str(value.get("id") or value.get("case_id") or "").strip()
        if not case_id:
            raise ValueError("case requires a non-empty id")
        return cls(
            case_id=case_id,
            payload=value.get("input", value.get("payload", {})),
            expected=value.get("expected", {}),
            scenario=str(value.get("scenario", "default")),
            metadata=value.get("metadata", {}),
            suite=suite,
        )


@dataclass(frozen=True)
class TraceEvent:
    module: str
    action: str
    status: str = "ok"
    duration_ms: float = 0
    error: str | None = None
    fields: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class NormalizedTrace:
    trace_id: str
    final_output: Any
    events: tuple[TraceEvent, ...] = ()
    fields: Mapping[str, Any] = field(default_factory=dict)
    feedback: Mapping[str, Any] = field(default_factory=dict)
    target_type: str = "agent"
    target_id: str = "default"
    target_version: str = "unknown"
    raw: Mapping[str, Any] = field(default_factory=dict)

    def as_mapping(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "final_output": self.final_output,
            "events": [asdict(event) for event in self.events],
            "fields": dict(self.fields),
            "feedback": dict(self.feedback),
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_version": self.target_version,
            "raw": dict(self.raw),
        }


@dataclass(frozen=True)
class Rule:
    name: str
    actual: str
    operator: str = "eq"
    expected: str | None = None
    value: Any = None
    message: str = ""
    suspected_modules: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunContext:
    run_id: str
    suite: str
    source: str
    attempt: int
    timeout_seconds: float


@dataclass(frozen=True)
class CheckResult:
    layer: str
    level: str
    name: str
    passed: bool
    actual: Any = None
    expected: Any = None
    message: str = ""
    suspected_modules: tuple[str, ...] = ()


@dataclass
class CaseResult:
    run_id: str
    suite: str
    source: str
    case: EvalCase
    trace: NormalizedTrace | None
    checks: list[CheckResult]
    error: str | None = None
    llm_review: Mapping[str, Any] = field(default_factory=dict)
    few_shot_candidate: Mapping[str, Any] = field(default_factory=dict)

    @property
    def hard_pass(self) -> bool:
        return self.error is None and all(
            check.passed for check in self.checks if check.level == "hard"
        )

    @property
    def soft_warning_count(self) -> int:
        return sum(
            1 for check in self.checks if check.level in {"soft", "candidate"} and not check.passed
        )


AgentCaller = Callable[[EvalCase, RunContext], Any]
TraceReader = Callable[[Any, EvalCase], NormalizedTrace]


@dataclass(frozen=True)
class ProjectAdapter:
    name: str
    call_agent: AgentCaller
    read_trace: TraceReader
    hard_gates: tuple[Rule, ...]
    soft_quality: tuple[Rule, ...]
    max_concurrency: int | None = None

    def __post_init__(self) -> None:
        if self.max_concurrency is not None and self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")


@dataclass(frozen=True)
class EvolutionDiagnosis:
    summary: str
    target_type: str
    target_id: str
    evidence_case_ids: tuple[str, ...] = ()
    suspected_modules: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TextCandidate:
    candidate_id: str
    candidate_version: str
    content: str
    summary: str
    change_type: str = "skill"
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EvolutionBudget:
    max_rounds: int = 3
    max_candidates_per_round: int = 3
    max_elapsed_seconds: float | None = None
    max_evolver_calls: int | None = None

    def __post_init__(self) -> None:
        if self.max_rounds < 1 or self.max_candidates_per_round < 1:
            raise ValueError("evolution budget values must be positive")
        if self.max_elapsed_seconds is not None and self.max_elapsed_seconds <= 0:
            raise ValueError("max_elapsed_seconds must be positive")
        if self.max_evolver_calls is not None and self.max_evolver_calls < 1:
            raise ValueError("max_evolver_calls must be positive")


class RetryableEvolverError(ValueError):
    """The evolver response was unusable, but repeating the call may recover."""


Diagnoser = Callable[[Mapping[str, Any]], EvolutionDiagnosis]
CandidateGenerator = Callable[[EvolutionDiagnosis, str, int], Sequence[TextCandidate]]
AdapterBuilder = Callable[[Path, str], ProjectAdapter]


@dataclass(frozen=True)
class AutoEvolutionAdapter:
    name: str
    baseline_artifact: Path
    baseline_version: str
    target_type: str
    target_id: str
    diagnose: Diagnoser
    generate_candidates: CandidateGenerator
    build_adapter: AdapterBuilder


@dataclass(frozen=True)
class EvolutionCandidate:
    candidate_id: str
    target_type: str
    target_id: str
    baseline_version: str
    candidate_version: str
    change_type: str
    artifact_ref: str = ""
    summary: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvolutionCandidate":
        required = (
            "candidate_id",
            "target_type",
            "target_id",
            "baseline_version",
            "candidate_version",
            "change_type",
        )
        missing = [name for name in required if not str(value.get(name, "")).strip()]
        if missing:
            raise ValueError(f"candidate requires: {', '.join(missing)}")
        return cls(
            candidate_id=str(value["candidate_id"]),
            target_type=str(value["target_type"]),
            target_id=str(value["target_id"]),
            baseline_version=str(value["baseline_version"]),
            candidate_version=str(value["candidate_version"]),
            change_type=str(value["change_type"]),
            artifact_ref=str(value.get("artifact_ref", "")),
            summary=str(value.get("summary", "")),
            metadata=value.get("metadata", {}),
        )


@dataclass(frozen=True)
class MetricObjective:
    name: str
    metric: str
    direction: str = "maximize"
    aggregation: str = "mean"
    minimum_improvement: float = 0
    maximum_regression: float = 0
    required: bool = True

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MetricObjective":
        name = str(value.get("name", "")).strip()
        metric = str(value.get("metric", "")).strip()
        if not name or not metric:
            raise ValueError("objective requires name and metric")
        direction = str(value.get("direction", "maximize"))
        aggregation = str(value.get("aggregation", "mean"))
        if direction not in {"maximize", "minimize"}:
            raise ValueError(f"unsupported objective direction: {direction}")
        if aggregation not in {"mean", "sum", "min", "max"}:
            raise ValueError(f"unsupported objective aggregation: {aggregation}")
        return cls(
            name=name,
            metric=metric,
            direction=direction,
            aggregation=aggregation,
            minimum_improvement=float(value.get("minimum_improvement", 0)),
            maximum_regression=float(value.get("maximum_regression", 0)),
            required=bool(value.get("required", True)),
        )


@dataclass(frozen=True)
class ScenarioGate:
    scenario: str
    roles: tuple[str, ...] = ("regression", "holdout")
    minimum_case_count: int = 1
    minimum_hard_pass_rate: float | None = None
    maximum_hard_pass_rate_drop: float = 0
    maximum_soft_warning_increase: int = 0

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScenarioGate":
        scenario = str(value.get("scenario", "")).strip()
        if not scenario:
            raise ValueError("scenario gate requires scenario")
        raw_roles = value.get("roles", ("regression", "holdout"))
        roles = (raw_roles,) if isinstance(raw_roles, str) else tuple(raw_roles)
        allowed_roles = {"improvement", "regression", "holdout"}
        if not roles or any(role not in allowed_roles for role in roles):
            raise ValueError(f"scenario gate has unsupported roles: {roles}")
        minimum_case_count = int(value.get("minimum_case_count", 1))
        minimum_hard_pass_rate = value.get("minimum_hard_pass_rate")
        maximum_hard_pass_rate_drop = float(
            value.get("maximum_hard_pass_rate_drop", 0)
        )
        maximum_soft_warning_increase = int(
            value.get("maximum_soft_warning_increase", 0)
        )
        if minimum_case_count < 1:
            raise ValueError("scenario gate minimum_case_count must be at least 1")
        if minimum_hard_pass_rate is not None:
            minimum_hard_pass_rate = float(minimum_hard_pass_rate)
            if not 0 <= minimum_hard_pass_rate <= 1:
                raise ValueError("scenario gate minimum_hard_pass_rate must be between 0 and 1")
        if not 0 <= maximum_hard_pass_rate_drop <= 1:
            raise ValueError(
                "scenario gate maximum_hard_pass_rate_drop must be between 0 and 1"
            )
        if maximum_soft_warning_increase < 0:
            raise ValueError(
                "scenario gate maximum_soft_warning_increase must not be negative"
            )
        return cls(
            scenario=scenario,
            roles=roles,
            minimum_case_count=minimum_case_count,
            minimum_hard_pass_rate=minimum_hard_pass_rate,
            maximum_hard_pass_rate_drop=maximum_hard_pass_rate_drop,
            maximum_soft_warning_increase=maximum_soft_warning_increase,
        )


@dataclass(frozen=True)
class EvolutionPolicy:
    require_regression_pass: bool = True
    require_holdout_pass: bool = False
    max_regression_soft_warning_increase: int = 0
    max_holdout_hard_pass_drop: float = 0
    max_holdout_soft_warning_increase: int = 0
    require_improvement: bool = True
    objectives: tuple[MetricObjective, ...] = ()
    scenario_gates: tuple[ScenarioGate, ...] = ()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EvolutionPolicy":
        return cls(
            require_regression_pass=bool(value.get("require_regression_pass", True)),
            require_holdout_pass=bool(value.get("require_holdout_pass", False)),
            max_regression_soft_warning_increase=int(
                value.get("max_regression_soft_warning_increase", 0)
            ),
            max_holdout_hard_pass_drop=float(value.get("max_holdout_hard_pass_drop", 0)),
            max_holdout_soft_warning_increase=int(
                value.get("max_holdout_soft_warning_increase", 0)
            ),
            require_improvement=bool(value.get("require_improvement", True)),
            objectives=tuple(
                MetricObjective.from_dict(item) for item in value.get("objectives", [])
            ),
            scenario_gates=tuple(
                ScenarioGate.from_dict(item) for item in value.get("scenario_gates", [])
            ),
        )


def to_jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: to_jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(item) for item in value]
    return value
