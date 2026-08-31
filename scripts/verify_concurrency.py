from __future__ import annotations

import argparse
import json
import platform
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_eval import EvalCase, NormalizedTrace, ProjectAdapter, Rule, RunContext, TraceEvent
from agent_eval.engine import EvaluationEngine
from agent_eval.store import ResultStore


def build_cases(count: int) -> list[EvalCase]:
    return [
        EvalCase(
            f"STRESS-{index:04d}",
            {"index": index},
            {"route": "OK"},
            "concurrency",
            suite="stress",
        )
        for index in range(count)
    ]


def build_adapter(permanent_failure_modulo: int | None = None) -> tuple[ProjectAdapter, dict[str, int]]:
    counters = {"transient_failures": 0, "permanent_failure_attempts": 0}
    lock = threading.Lock()

    def call_agent(case: EvalCase, context: RunContext) -> dict[str, float | str]:
        index = int(case.payload["index"])
        if permanent_failure_modulo and index % permanent_failure_modulo == 0:
            with lock:
                counters["permanent_failure_attempts"] += 1
            raise RuntimeError("injected permanent failure")
        if index % 100 == 0 and context.attempt == 0:
            with lock:
                counters["transient_failures"] += 1
            raise RuntimeError("injected transient failure")
        started = time.perf_counter()
        time.sleep(0.001)
        return {
            "trace_id": f"{context.run_id}-{case.case_id}",
            "route": "OK",
            "latency_ms": (time.perf_counter() - started) * 1000,
        }

    def read_trace(handle: dict[str, float | str], _: EvalCase) -> NormalizedTrace:
        latency = float(handle["latency_ms"])
        return NormalizedTrace(
            str(handle["trace_id"]),
            {"route": handle["route"]},
            (TraceEvent("SyntheticAgent", "route", duration_ms=latency),),
            {"route": handle["route"], "latency_ms": latency},
        )

    return (
        ProjectAdapter(
            "synthetic-concurrency",
            call_agent,
            read_trace,
            (Rule("route", "fields.route", expected="route"),),
            (),
        ),
        counters,
    )


def run_profile(database: Path, workers: int, count: int) -> dict[str, float | int | str]:
    adapter, counters = build_adapter()
    tracemalloc.reset_peak()
    started = time.perf_counter()
    with ResultStore(database) as store:
        result = EvaluationEngine(adapter, store, workers=workers, retries=1).run_suite(
            build_cases(count),
            "stress",
            run_id=f"stress-workers-{workers}",
        )
        unique_cases = store.connection.execute(
            "SELECT COUNT(DISTINCT case_id) FROM case_results WHERE run_id = ?",
            (result["run_id"],),
        ).fetchone()[0]
    elapsed = time.perf_counter() - started
    _, peak_bytes = tracemalloc.get_traced_memory()
    latencies = sorted(
        float(item["trace"]["fields"]["latency_ms"])
        for item in result["results"]
        if item.get("trace")
    )
    return {
        "workers": workers,
        "case_count": result["case_count"],
        "unique_case_count": unique_cases,
        "status": result["status"],
        "hard_failures": result["hard_failures"],
        "injected_transient_failures": counters["transient_failures"],
        "elapsed_seconds": round(elapsed, 4),
        "throughput_cases_per_second": round(count / elapsed, 2),
        "mean_agent_latency_ms": round(statistics.mean(latencies), 4),
        "p95_agent_latency_ms": round(latencies[int(0.95 * len(latencies)) - 1], 4),
        "peak_traced_memory_bytes": peak_bytes,
    }


def run_recovery_profile(database: Path, count: int) -> dict[str, int | str]:
    adapter, counters = build_adapter()
    cases = build_cases(count)
    with ResultStore(database) as store:
        engine = EvaluationEngine(adapter, store, workers=8, retries=1)
        engine.run_suite(cases[: count // 2], "stress", run_id="recovery")
        recovered = engine.run_suite(cases, "stress", run_id="recovery", resume=True)
        unique_cases = store.connection.execute(
            "SELECT COUNT(DISTINCT case_id) FROM case_results WHERE run_id = 'recovery'"
        ).fetchone()[0]
    return {
        "status": recovered["status"],
        "case_count": recovered["case_count"],
        "unique_case_count": unique_cases,
        "transient_failures": counters["transient_failures"],
    }


def run_failure_profile(database: Path, count: int) -> dict[str, int | str]:
    adapter, counters = build_adapter(permanent_failure_modulo=200)
    with ResultStore(database) as store:
        result = EvaluationEngine(adapter, store, workers=8, retries=1).run_suite(
            build_cases(count),
            "stress",
            run_id="permanent-failures",
        )
        unique_cases = store.connection.execute(
            "SELECT COUNT(DISTINCT case_id) FROM case_results WHERE run_id = 'permanent-failures'"
        ).fetchone()[0]
    expected_hard_failures = count // 200
    expected_attempts = expected_hard_failures * 2
    verification_passed = (
        result["status"] == "failed"
        and result["case_count"] == count
        and unique_cases == count
        and result["hard_failures"] == expected_hard_failures
        and counters["permanent_failure_attempts"] == expected_attempts
    )
    return {
        "verification_status": "passed" if verification_passed else "failed",
        "agent_run_status": result["status"],
        "case_count": result["case_count"],
        "unique_case_count": unique_cases,
        "hard_failures": result["hard_failures"],
        "permanent_failure_attempts": counters["permanent_failure_attempts"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the no-key concurrency evidence suite")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--cases", type=int, default=1000)
    args = parser.parse_args()
    if args.cases < 1000:
        raise SystemExit("concurrency evidence requires at least 1000 cases")

    tracemalloc.start()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        profiles = [
            run_profile(root / "concurrency.db", workers, args.cases)
            for workers in (1, 8, 32)
        ]
        recovery = run_recovery_profile(root / "recovery.db", args.cases)
        failure_accounting = run_failure_profile(root / "failures.db", args.cases)

    passed = all(
        profile["status"] == "passed"
        and profile["case_count"] == args.cases
        and profile["unique_case_count"] == args.cases
        and profile["hard_failures"] == 0
        and profile["injected_transient_failures"] == args.cases // 100
        for profile in profiles
    ) and recovery == {
        "status": "passed",
        "case_count": args.cases,
        "unique_case_count": args.cases,
        "transient_failures": args.cases // 100,
    } and failure_accounting == {
        "verification_status": "passed",
        "agent_run_status": "failed",
        "case_count": args.cases,
        "unique_case_count": args.cases,
        "hard_failures": args.cases // 200,
        "permanent_failure_attempts": (args.cases // 200) * 2,
    }
    evidence = {
        "schema_version": 2,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "status": "passed" if passed else "failed",
        "case_count_per_profile": args.cases,
        "profiles": profiles,
        "resume_recovery": recovery,
        "permanent_failure_accounting": failure_accounting,
        "scope": "single-machine bounded case concurrency with deterministic transient failures",
        "limitations": [
            "This does not measure a real business service capacity.",
            "Peak memory uses Python tracemalloc and is not total process RSS.",
            "Distributed scheduling and multi-host database behavior are out of scope.",
        ],
    }
    content = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    print(content)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
