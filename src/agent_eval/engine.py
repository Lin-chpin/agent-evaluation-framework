from __future__ import annotations

import importlib.util
import json
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable, Mapping, Sequence

from .llm import OpenAICompatibleReviewer
from .model import (
    CaseResult,
    CheckResult,
    EvalCase,
    NormalizedTrace,
    ProjectAdapter,
    RunContext,
)
from .rules import behavior_checks, consistency_checks, feedback_checks, structure_checks
from .store import ResultStore


def load_cases(path: Path, suite: str) -> list[EvalCase]:
    cases: list[EvalCase] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                cases.append(EvalCase.from_dict(json.loads(line), suite))
            except (ValueError, json.JSONDecodeError) as error:
                raise ValueError(f"invalid case at {path}:{line_number}: {error}") from error
    if not cases:
        raise ValueError(f"case file is empty: {path}")
    return cases


def load_adapter(path: Path) -> ProjectAdapter:
    spec = importlib.util.spec_from_file_location("agent_eval_project_adapter", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load adapter: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    adapter = getattr(module, "ADAPTER", None)
    if not isinstance(adapter, ProjectAdapter):
        raise TypeError(f"{path} must export ADAPTER = ProjectAdapter(...)")
    return adapter


class EvaluationEngine:
    def __init__(
        self,
        adapter: ProjectAdapter,
        store: ResultStore,
        reviewer: OpenAICompatibleReviewer | None = None,
        workers: int = 1,
        retries: int = 0,
        timeout_seconds: float = 30,
        collect_few_shot: bool = False,
    ):
        self.adapter = adapter
        self.store = store
        self.reviewer = reviewer
        self.requested_workers = max(1, workers)
        self.workers = min(
            self.requested_workers,
            adapter.max_concurrency or self.requested_workers,
        )
        self.max_in_flight = self.workers * 2
        self.retries = max(0, retries)
        self.timeout_seconds = timeout_seconds
        self.collect_few_shot = collect_few_shot

    def _read_one_trace(
        self,
        case: EvalCase,
        run_id: str,
        source: str,
        attempt: int,
    ) -> NormalizedTrace:
        context = RunContext(run_id, case.suite, source, attempt, self.timeout_seconds)
        if source == "online":
            # ponytail: the adapter enforces hard cancellation; use process isolation if runners can hang.
            handle = self.adapter.call_agent(case, context)
        elif source == "offline":
            handle = case.metadata.get("trace_ref", case.metadata.get("trace"))
            if handle is None:
                raise ValueError(f"offline case {case.case_id} requires metadata.trace_ref or metadata.trace")
        else:
            raise ValueError(f"unsupported source: {source}")
        trace = self.adapter.read_trace(handle, case)
        if not isinstance(trace, NormalizedTrace):
            raise TypeError("read_trace must return NormalizedTrace")
        return trace

    def _collect_traces(self, case: EvalCase, run_id: str, source: str) -> list[NormalizedTrace]:
        consistency_runs = int(case.metadata.get("consistency_runs", 1))
        if case.metadata.get("consistency_check") is True:
            consistency_runs = max(2, consistency_runs)
        traces: list[NormalizedTrace] = []
        for run_index in range(consistency_runs):
            last_error: Exception | None = None
            for attempt in range(self.retries + 1):
                try:
                    traces.append(self._read_one_trace(case, run_id, source, attempt))
                    last_error = None
                    break
                except Exception as error:  # adapter boundary: preserve the external failure
                    last_error = error
            if last_error is not None:
                raise last_error
        return traces

    def _evaluate_case(self, case: EvalCase, run_id: str, source: str) -> CaseResult:
        try:
            traces = self._collect_traces(case, run_id, source)
            trace = traces[0]
            checks = structure_checks(case, trace, self.adapter.hard_gates, self.adapter.soft_quality)
            checks.extend(behavior_checks(case, trace))
            checks.extend(consistency_checks(traces, self.adapter.hard_gates + self.adapter.soft_quality))
            checks.extend(feedback_checks(trace))
            result = CaseResult(run_id, case.suite, source, case, trace, checks)

            should_review = any(not check.passed for check in checks) or self.collect_few_shot
            if self.reviewer and should_review:
                try:
                    result.llm_review = self.reviewer.analyze(
                        case,
                        traces,
                        checks,
                        self.collect_few_shot and result.hard_pass and result.soft_warning_count == 0,
                    )
                except Exception as error:  # LLM analysis never changes evaluation truth
                    result.llm_review = {"error": str(error), "authoritative": False}

            if self.collect_few_shot and result.hard_pass and result.soft_warning_count == 0:
                result.few_shot_candidate = {
                    "status": "pending_human_review",
                    "source_case_id": case.case_id,
                    "target_type": trace.target_type,
                    "target_id": trace.target_id,
                    "target_version": trace.target_version,
                    "scenario": case.scenario,
                    "input": case.payload,
                    "decision_path": [
                        {"module": event.module, "action": event.action} for event in trace.events
                    ],
                    "preferred_output": trace.final_output,
                    "llm_candidate": result.llm_review.get("few_shot_candidate"),
                }
            return result
        except Exception as error:
            return CaseResult(
                run_id,
                case.suite,
                source,
                case,
                None,
                [
                    CheckResult(
                        "structure",
                        "hard",
                        "execution_error",
                        False,
                        str(error),
                        "successful execution and readable trace",
                        "framework or adapter execution failed",
                    )
                ],
                error=str(error),
            )

    def run_suite(
        self,
        cases: Sequence[EvalCase],
        suite: str,
        source: str = "online",
        run_id: str | None = None,
        resume: bool = False,
    ) -> dict[str, Any]:
        run_id = run_id or f"{suite}-{uuid.uuid4().hex[:12]}"
        with self.store.lock_run(run_id):
            return self._run_suite_locked(cases, suite, source, run_id, resume)

    def _run_suite_locked(
        self,
        cases: Sequence[EvalCase],
        suite: str,
        source: str,
        run_id: str,
        resume: bool,
    ) -> dict[str, Any]:
        self.store.start_run(
            run_id,
            self.adapter.name,
            suite,
            source,
            {
                "case_count": len(cases),
                "workers": self.workers,
                "requested_workers": self.requested_workers,
                "max_in_flight": self.max_in_flight,
                "retries": self.retries,
                "timeout_seconds": self.timeout_seconds,
                "collect_few_shot": self.collect_few_shot,
            },
            resume=resume,
        )
        pending = [
            case for case in cases if not (resume and self.store.has_case(run_id, case.case_id))
        ]
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            remaining = iter(pending)
            futures = {
                pool.submit(self._evaluate_case, case, run_id, source)
                for case in (next(remaining, None) for _ in range(self.max_in_flight))
                if case is not None
            }
            while futures:
                completed, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in completed:
                    self.store.save_case(future.result())
                    case = next(remaining, None)
                    if case is not None:
                        futures.add(pool.submit(self._evaluate_case, case, run_id, source))

        results = self.store.list_results(run_id)
        hard_failures = sum(1 for result in results if not result["hard_pass"])
        soft_warnings = sum(result["soft_warning_count"] for result in results)
        status = "failed" if hard_failures else "passed"
        self.store.finish_run(run_id, status)
        return {
            "run_id": run_id,
            "suite": suite,
            "source": source,
            "status": status,
            "case_count": len(results),
            "hard_failures": hard_failures,
            "soft_warnings": soft_warnings,
            "results": results,
        }

    def run_release(
        self,
        suites: Sequence[tuple[str, Sequence[EvalCase]]],
        source: str = "online",
        release_id: str | None = None,
    ) -> dict[str, Any]:
        release_id = release_id or f"release-{uuid.uuid4().hex[:12]}"
        stages: list[dict[str, Any]] = []
        for suite, cases in suites:
            summary = self.run_suite(
                cases,
                suite,
                source=source,
                run_id=f"{release_id}-{suite}",
            )
            stages.append(summary)
            if summary["hard_failures"]:
                break
        return {
            "release_id": release_id,
            "status": "failed" if any(stage["hard_failures"] for stage in stages) else "passed",
            "stages": stages,
        }
