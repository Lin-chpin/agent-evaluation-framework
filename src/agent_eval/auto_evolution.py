from __future__ import annotations

import importlib.util
import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .engine import EvaluationEngine
from .evolution import EvolutionEngine
from .model import (
    AutoEvolutionAdapter,
    EvalCase,
    EvolutionBudget,
    EvolutionCandidate,
    EvolutionDiagnosis,
    EvolutionPolicy,
    ProjectAdapter,
    RetryableEvolverError,
    TextCandidate,
    TextFileOperation,
    to_jsonable,
)
from .store import ResultStore
from .workspace import TextArtifactWorkspace, read_artifact, safe_name


class _TimeBudgetExhausted(Exception):
    pass


class _EvolverCallBudgetExhausted(Exception):
    pass


def load_auto_evolution_adapter(path: Path) -> AutoEvolutionAdapter:
    spec = importlib.util.spec_from_file_location("agent_eval_auto_evolution_adapter", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load auto evolution adapter: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    adapter = getattr(module, "AUTO_EVOLUTION", None)
    if not isinstance(adapter, AutoEvolutionAdapter):
        raise TypeError(f"{path} must export AUTO_EVOLUTION = AutoEvolutionAdapter(...)")
    return adapter


def _dataset_manifest(datasets: Mapping[str, Sequence[EvalCase]]) -> dict[str, Any]:
    manifest: dict[str, Any] = {}
    identity_fields = (
        "evaluated_target_id",
        "evaluated_target_version",
        "evaluator_skill_id",
        "evaluator_skill_version",
    )
    for role, cases in sorted(datasets.items()):
        canonical = json.dumps(
            [to_jsonable(case) for case in cases],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        identity = {}
        for field in identity_fields:
            values = sorted(
                {
                    str(case.metadata[field])
                    for case in cases
                    if case.metadata.get(field) is not None
                }
            )
            if values:
                identity[field] = values
        manifest[role] = {
            "case_count": len(cases),
            "sha256": hashlib.sha256(canonical).hexdigest().upper(),
            **identity,
        }
    return manifest


def _artifact_sha256(path: Path) -> str:
    return hashlib.sha256(
        json.dumps(read_artifact(path), ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class AutoEvolutionLoop:
    def __init__(
        self,
        store: ResultStore,
        workspace: TextArtifactWorkspace,
        policy: EvolutionPolicy | None = None,
        workers: int = 1,
        retries: int = 0,
        timeout_seconds: float = 30,
    ):
        self.store = store
        self.workspace = workspace
        self.policy = policy or EvolutionPolicy()
        self.workers = workers
        self.retries = retries
        self.timeout_seconds = timeout_seconds

    def _engine(self, adapter: ProjectAdapter, artifact: Path) -> EvaluationEngine:
        return EvaluationEngine(
            adapter,
            self.store,
            workers=self.workers,
            retries=self.retries,
            timeout_seconds=self.timeout_seconds,
            run_identity=_artifact_sha256(artifact),
        )

    @staticmethod
    def _validate_diagnosis(
        diagnosis: EvolutionDiagnosis, adapter: AutoEvolutionAdapter
    ) -> None:
        if diagnosis.target_type != adapter.target_type or diagnosis.target_id != adapter.target_id:
            raise ValueError("diagnosis target does not match auto evolution adapter")

    @staticmethod
    def _validate_candidate(candidate: TextCandidate, baseline_version: str) -> None:
        safe_name(candidate.candidate_id, "candidate_id")
        safe_name(candidate.candidate_version, "candidate_version")
        if candidate.candidate_version == baseline_version:
            raise ValueError("candidate version must differ from baseline version")
        if candidate.files and candidate.operations:
            raise ValueError("candidate must use files or operations, not both")
        if not candidate.content.strip() and not candidate.files and not candidate.operations:
            raise ValueError("candidate content, files, or operations must not be empty")

    def run(
        self,
        adapter: AutoEvolutionAdapter,
        datasets: Mapping[str, Sequence[EvalCase]],
        budget: EvolutionBudget | None = None,
        loop_id: str | None = None,
        source: str = "online",
        resume: bool = False,
    ) -> dict[str, Any]:
        if resume and not loop_id:
            raise ValueError("resume requires loop_id")
        loop_id = loop_id or f"auto-evolution-{uuid.uuid4().hex[:12]}"
        with self.workspace.lock_loop(loop_id):
            return self._run_locked(adapter, datasets, budget, loop_id, source, resume)

    def _run_locked(
        self,
        adapter: AutoEvolutionAdapter,
        datasets: Mapping[str, Sequence[EvalCase]],
        budget: EvolutionBudget | None,
        loop_id: str,
        source: str,
        resume: bool,
    ) -> dict[str, Any]:
        if source != "online":
            raise ValueError("automatic evolution requires fresh online evaluation")
        required = {"improvement", "regression", "holdout"}
        missing = required.difference(datasets)
        if missing or any(not datasets.get(role) for role in required):
            raise ValueError("automatic evolution requires non-empty improvement, regression, and holdout datasets")
        if not adapter.baseline_artifact.exists():
            raise ValueError(f"baseline artifact does not exist: {adapter.baseline_artifact}")

        budget = budget or EvolutionBudget()
        dataset_manifest = _dataset_manifest(datasets)
        started = time.monotonic()
        previous_elapsed = 0.0
        evolver_calls = 0
        evolver_retries = 0

        if resume:
            checkpoint = self.workspace.load_checkpoint(loop_id)
            if (
                checkpoint.get("target_type") != adapter.target_type
                or checkpoint.get("target_id") != adapter.target_id
                or checkpoint.get("initial_version") != adapter.baseline_version
            ):
                raise ValueError("checkpoint target does not match auto evolution adapter")
            if checkpoint.get("datasets") not in (None, dataset_manifest):
                raise ValueError("checkpoint datasets do not match the current frozen datasets")
            if checkpoint.get("policy") != to_jsonable(self.policy):
                raise ValueError("checkpoint policy does not match the current frozen policy")
            current_path = Path(checkpoint["current_artifact"])
            if not current_path.exists():
                raise FileNotFoundError(f"checkpoint artifact does not exist: {current_path}")
            if checkpoint.get("current_artifact_sha256") != _artifact_sha256(current_path):
                raise ValueError("checkpoint artifact content changed")
            if checkpoint.get("status") == "completed":
                return checkpoint
            current_version = str(checkpoint["current_version"])
            rounds = list(checkpoint.get("rounds", []))
            pending_round = checkpoint.get("pending_round")
            usage = checkpoint.get("usage", {})
            previous_elapsed = float(usage.get("elapsed_seconds", 0))
            evolver_calls = int(usage.get("evolver_calls", 0))
            evolver_retries = int(usage.get("evolver_retries", 0))
        else:
            current_path = self.workspace.snapshot(loop_id, adapter.baseline_artifact)
            current_version = adapter.baseline_version
            rounds: list[dict[str, Any]] = []
            pending_round = None

        phase = "initialization"

        def elapsed_seconds() -> float:
            return previous_elapsed + time.monotonic() - started

        def state(status: str, error: str | None = None) -> dict[str, Any]:
            result = {
                "loop_id": loop_id,
                "status": status,
                "target_type": adapter.target_type,
                "target_id": adapter.target_id,
                "initial_version": adapter.baseline_version,
                "current_version": current_version,
                "current_artifact": str(current_path),
                "current_artifact_sha256": _artifact_sha256(current_path),
                "datasets": dataset_manifest,
                "policy": to_jsonable(self.policy),
                "budget": to_jsonable(budget),
                "usage": {
                    "elapsed_seconds": round(elapsed_seconds(), 3),
                    "evolver_calls": evolver_calls,
                    "evolver_retries": evolver_retries,
                },
                "rounds": rounds,
            }
            if error is not None:
                result["failed_phase"] = phase
                result["error"] = error
            if pending_round is not None:
                result["pending_round"] = pending_round
            return result

        def save(status: str, error: str | None = None) -> dict[str, Any]:
            result = state(status, error)
            self.workspace.save_checkpoint(loop_id, result)
            return result

        def time_exhausted() -> bool:
            return (
                budget.max_elapsed_seconds is not None
                and elapsed_seconds() >= budget.max_elapsed_seconds
            )

        def evolver_exhausted() -> bool:
            return (
                budget.max_evolver_calls is not None
                and evolver_calls >= budget.max_evolver_calls
            )

        def invoke_evolver(action: Any) -> Any:
            nonlocal evolver_calls, evolver_retries
            for attempt in range(2):
                if time_exhausted():
                    raise _TimeBudgetExhausted
                if evolver_exhausted():
                    raise _EvolverCallBudgetExhausted
                evolver_calls += 1
                try:
                    return action()
                except RetryableEvolverError:
                    if attempt:
                        raise
                    evolver_retries += 1
            raise AssertionError("unreachable")

        save("running")
        try:
            for round_number in range(len(rounds) + 1, budget.max_rounds + 1):
                if time_exhausted():
                    return save("time_budget_exhausted")
                if rounds and pending_round is None and evolver_exhausted():
                    return save("evolver_call_budget_exhausted")

                phase = "baseline_evaluation"
                evaluation_path = Path(pending_round["baseline_artifact"]) if pending_round else current_path
                evaluation_version = pending_round["baseline_version"] if pending_round else current_version
                baseline_engine = self._engine(
                    adapter.build_adapter(evaluation_path, evaluation_version), evaluation_path
                )
                if pending_round is None:
                    diagnosis_run = baseline_engine.run_suite(
                        datasets["improvement"],
                        "improvement",
                        run_id=f"{loop_id}-round-{round_number}-diagnosis",
                        resume=resume,
                    )
                    if not diagnosis_run["hard_failures"] and not diagnosis_run["soft_warnings"]:
                        return save("completed")
                    if time_exhausted():
                        return save("time_budget_exhausted")
                    if evolver_exhausted():
                        return save("evolver_call_budget_exhausted")

                    phase = "diagnosis"
                    diagnosis = invoke_evolver(lambda: adapter.diagnose(diagnosis_run))
                    self._validate_diagnosis(diagnosis, adapter)
                    if time_exhausted():
                        return save("time_budget_exhausted")
                    if evolver_exhausted():
                        return save("evolver_call_budget_exhausted")

                    phase = "candidate_generation"
                    proposals = list(
                        invoke_evolver(
                            lambda: adapter.generate_candidates(
                                diagnosis,
                                read_artifact(evaluation_path),
                                round_number,
                            )
                        )
                    )[: budget.max_candidates_per_round]
                    if not proposals:
                        rounds.append(
                            {
                                "round": round_number,
                                "diagnosis": to_jsonable(diagnosis),
                                "diagnosis_run": diagnosis_run,
                                "candidates": [],
                            }
                        )
                        return save("no_candidates")
                    pending_round = {
                        "round": round_number,
                        "baseline_artifact": str(evaluation_path),
                        "baseline_version": evaluation_version,
                        "diagnosis": to_jsonable(diagnosis),
                        "diagnosis_run": diagnosis_run,
                        "proposals": [to_jsonable(candidate) for candidate in proposals],
                        "candidates": [],
                    }
                    save("running")
                else:
                    if pending_round["round"] != round_number:
                        raise ValueError("checkpoint pending round does not match the next round")
                    diagnosis_run = pending_round["diagnosis_run"]
                    diagnosis = EvolutionDiagnosis(**pending_round["diagnosis"])
                    proposals = [
                        TextCandidate(
                            **{
                                **value,
                                "operations": tuple(
                                    TextFileOperation(**operation)
                                    for operation in value.get("operations", [])
                                ),
                            }
                        )
                        for value in pending_round["proposals"]
                    ]

                candidate_results = pending_round["candidates"]
                accepted = bool(candidate_results and candidate_results[-1]["evaluation"]["decision"] == "accept")
                for candidate in proposals[len(candidate_results):] if not accepted else ():
                    if time_exhausted():
                        return save("time_budget_exhausted")
                    self._validate_candidate(candidate, evaluation_version)
                    candidate_path = self.workspace.stage(
                        loop_id,
                        round_number,
                        candidate,
                        adapter.baseline_artifact.name,
                        evaluation_path,
                    )
                    change = EvolutionCandidate(
                        candidate.candidate_id,
                        adapter.target_type,
                        adapter.target_id,
                        evaluation_version,
                        candidate.candidate_version,
                        candidate.change_type,
                        artifact_ref=str(candidate_path),
                        summary=candidate.summary,
                        metadata=candidate.metadata,
                    )
                    phase = "candidate_evaluation"
                    result = EvolutionEngine(
                        baseline_engine,
                        self._engine(
                            adapter.build_adapter(candidate_path, candidate.candidate_version), candidate_path
                        ),
                        self.policy,
                    ).run(
                        change,
                        datasets,
                        experiment_id=(
                            f"{loop_id}-round-{round_number}-{candidate.candidate_id}"
                        ),
                        resume=resume,
                    )
                    candidate_results.append(
                        {
                            "candidate": to_jsonable(candidate),
                            "artifact_path": str(candidate_path),
                            "evaluation": result,
                        }
                    )
                    # ponytail: first acceptable candidate wins; rank candidates when real workloads need it.
                    if result["decision"] == "accept":
                        current_path = candidate_path
                        current_version = candidate.candidate_version
                        accepted = True
                    save("running")
                    if accepted:
                        break

                rounds.append(
                    {
                        "round": round_number,
                        "diagnosis": to_jsonable(diagnosis),
                        "diagnosis_run": diagnosis_run,
                        "candidates": candidate_results,
                    }
                )
                pending_round = None
                save("running")
                if not accepted:
                    return save("no_acceptable_candidate")

                accepted_improvement = candidate_results[-1]["evaluation"]["candidate_runs"][
                    "improvement"
                ]
                if not accepted_improvement["hard_failures"] and not accepted_improvement[
                    "soft_warnings"
                ]:
                    return save("completed")

            return save("budget_exhausted")
        except _TimeBudgetExhausted:
            return save("time_budget_exhausted")
        except _EvolverCallBudgetExhausted:
            return save("evolver_call_budget_exhausted")
        except Exception as error:
            return save("failed", str(error))
