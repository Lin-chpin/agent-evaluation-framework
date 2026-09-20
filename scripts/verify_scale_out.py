from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import platform
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_eval import EvalCase, NormalizedTrace, ProjectAdapter, Rule, RunContext, TraceEvent
from agent_eval.engine import EvaluationEngine, load_adapter, load_cases
from agent_eval.store import ResultStore


PROFILES = {
    "short_io": {"base_delay_ms": 1.0, "trace_bytes": 0},
    "long_io": {"base_delay_ms": 20.0, "trace_bytes": 0},
    "trace_heavy": {"base_delay_ms": 5.0, "trace_bytes": 8192},
    "mixed_io": {"base_delay_ms": 1.0, "trace_bytes": 1024},
}


def parse_int_list(value: str) -> list[int]:
    values = [int(item.strip()) for item in value.split(",") if item.strip()]
    if not values or any(item < 1 for item in values):
        raise argparse.ArgumentTypeError("expected a comma-separated list of positive integers")
    return values


class RateGate:
    def __init__(self, rate_per_second: float) -> None:
        self.interval = 1 / rate_per_second if rate_per_second > 0 else 0
        self.next_start = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        if not self.interval:
            return
        # ponytail: one per-process gate; distributed rate shaping belongs to the load generator.
        with self.lock:
            scheduled = max(self.next_start, time.perf_counter())
            self.next_start = scheduled + self.interval
        delay = scheduled - time.perf_counter()
        if delay > 0:
            time.sleep(delay)


def build_cases(process_id: int, count: int) -> list[EvalCase]:
    return [
        EvalCase(
            f"P{process_id:02d}-{index:05d}",
            {"index": index, "process_id": process_id},
            {"route": "OK"},
            suite="scale-out",
        )
        for index in range(count)
    ]


def build_adapter(profile: str, arrival_rate_per_second: float) -> tuple[ProjectAdapter, dict[str, int]]:
    settings = PROFILES[profile]
    counters = {"transient_failures": 0}
    counter_lock = threading.Lock()
    rate_gate = RateGate(arrival_rate_per_second)

    def call_agent(case: EvalCase, context: RunContext) -> dict[str, float | str]:
        index = int(case.payload["index"])
        rate_gate.wait()
        if index % 100 == 0 and context.attempt == 0:
            with counter_lock:
                counters["transient_failures"] += 1
            raise RuntimeError("injected transient failure")

        delay_ms = settings["base_delay_ms"]
        trace_bytes = int(settings["trace_bytes"])
        if profile == "mixed_io":
            delay_ms *= 1 + (index % 10)
            trace_bytes *= 1 + (index % 8)
        started = time.perf_counter()
        time.sleep(delay_ms / 1000)
        return {
            "trace_id": f"{context.run_id}-{case.case_id}",
            "route": "OK",
            "latency_ms": (time.perf_counter() - started) * 1000,
            "trace_payload": "x" * trace_bytes,
        }

    def read_trace(handle: dict[str, float | str], _: EvalCase) -> NormalizedTrace:
        latency = float(handle["latency_ms"])
        payload = str(handle["trace_payload"])
        return NormalizedTrace(
            str(handle["trace_id"]),
            {"route": handle["route"]},
            (TraceEvent(
                "SyntheticUpstream",
                "invoke",
                duration_ms=latency,
                fields={"payload_bytes": len(payload)},
            ),),
            {"route": handle["route"], "latency_ms": latency, "trace_bytes": len(payload)},
            raw={"payload": payload} if payload else {},
        )

    return (
        ProjectAdapter(
            f"synthetic-scale-out-{profile}",
            call_agent,
            read_trace,
            (Rule("route", "fields.route", expected="route"),),
            (),
        ),
        counters,
    )


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def trace_latency(result: dict[str, Any]) -> float | None:
    trace = result.get("trace") or {}
    fields = trace.get("fields") or {}
    if isinstance(fields.get("latency_ms"), (int, float)):
        return float(fields["latency_ms"])
    events = trace.get("events") or []
    durations = [event.get("duration_ms", 0) for event in events]
    return sum(float(duration) for duration in durations) if durations else None


def rate_limit_adapter(adapter: ProjectAdapter, rate_per_second: float) -> ProjectAdapter:
    if rate_per_second <= 0:
        return adapter
    gate = RateGate(rate_per_second)

    def call_agent(case: EvalCase, context: RunContext) -> Any:
        gate.wait()
        return adapter.call_agent(case, context)

    return ProjectAdapter(
        adapter.name,
        call_agent,
        adapter.read_trace,
        adapter.hard_gates,
        adapter.soft_quality,
        adapter.max_concurrency,
    )


def run_process(
    database: str,
    run_id: str,
    profile: str,
    process_id: int,
    process_count: int,
    workers: int,
    cases_per_process: int,
    arrival_rate_per_second: float,
    adapter_path: str | None,
    cases_path: str | None,
    start: Any,
    results: Any,
) -> None:
    try:
        start.wait(120)
        tracemalloc.start()
        if adapter_path and cases_path:
            adapter = rate_limit_adapter(
                load_adapter(Path(adapter_path)), arrival_rate_per_second
            )
            cases = load_cases(Path(cases_path), "scale-out")[process_id::process_count]
            counters = {"transient_failures": 0}
        else:
            adapter, counters = build_adapter(profile, arrival_rate_per_second)
            cases = build_cases(process_id, cases_per_process)
        started = time.perf_counter()
        with ResultStore(Path(database)) as store:
            result = EvaluationEngine(adapter, store, workers=workers, retries=1).run_suite(
                cases,
                "scale-out",
                run_id=run_id,
            )
            unique_cases = store.connection.execute(
                "SELECT COUNT(DISTINCT case_id) FROM case_results WHERE run_id = ?",
                (run_id,),
            ).fetchone()[0]
        elapsed = time.perf_counter() - started
        _, peak_memory = tracemalloc.get_traced_memory()
        latencies = [
            latency
            for item in result["results"]
            for latency in [trace_latency(item)]
            if latency is not None
        ]
        p95 = percentile(latencies, 0.95)
        results.put({
            "run_id": run_id,
            "process_id": process_id,
            "status": result["status"],
            "case_count": result["case_count"],
            "unique_case_count": unique_cases,
            "hard_failures": result["hard_failures"],
            "injected_transient_failures": counters["transient_failures"],
            "elapsed_seconds": round(elapsed, 4),
            "throughput_cases_per_second": round(len(cases) / elapsed, 2),
            "mean_agent_latency_ms": round(statistics.mean(latencies), 4) if latencies else None,
            "p95_agent_latency_ms": round(p95, 4) if p95 is not None else None,
            "peak_traced_memory_bytes": peak_memory,
        })
    except Exception as error:
        results.put({
            "run_id": run_id,
            "process_id": process_id,
            "status": "error",
            "error": f"{type(error).__name__}: {error}",
        })
        raise


def run_topology(
    database: Path,
    profile: str,
    process_count: int,
    workers: int,
    cases_per_process: int,
    arrival_rate_per_process: float,
    timeout_seconds: float,
    max_p95_agent_ms: float | None,
    min_throughput: float | None,
    total_case_count: int,
    adapter_path: str | None,
    cases_path: str | None,
) -> dict[str, Any]:
    context = multiprocessing.get_context("spawn")
    start = context.Event()
    results_queue = context.Queue()
    run_ids = [f"scaleout-{profile}-p{process_id}" for process_id in range(process_count)]
    processes = [
        context.Process(
            target=run_process,
            args=(
                str(database),
                run_id,
                profile,
                process_id,
                process_count,
                workers,
                cases_per_process,
                arrival_rate_per_process,
                adapter_path,
                cases_path,
                start,
                results_queue,
            ),
        )
        for process_id, run_id in enumerate(run_ids)
    ]

    with ResultStore(database):
        pass
    started = time.perf_counter()
    for process in processes:
        process.start()
    start.set()
    deadline = time.monotonic() + timeout_seconds
    for process in processes:
        process.join(max(0, deadline - time.monotonic()))
    elapsed = time.perf_counter() - started

    summaries: list[dict[str, Any]] = []
    for _ in processes:
        try:
            summaries.append(results_queue.get(timeout=5))
        except Exception:
            break

    timed_out = [process.pid for process in processes if process.is_alive()]
    for process in processes:
        if process.is_alive():
            process.terminate()
            process.join(5)

    external_mode = bool(adapter_path and cases_path)
    expected_cases = total_case_count if external_mode else process_count * cases_per_process
    expected_transient_failures = (
        0
        if external_mode
        else process_count * ((cases_per_process - 1) // 100 + 1)
    )
    total_unique = sum(summary.get("unique_case_count", 0) for summary in summaries)
    total_hard_failures = sum(summary.get("hard_failures", 0) for summary in summaries)
    total_transient_failures = sum(
        summary.get("injected_transient_failures", 0) for summary in summaries
    )
    integrity_passed = (
        not timed_out
        and len(summaries) == process_count
        and all(process.exitcode == 0 for process in processes)
        and all(summary.get("status") == "passed" for summary in summaries)
        and sum(summary.get("case_count", 0) for summary in summaries) == expected_cases
        and total_unique == expected_cases
        and total_hard_failures == 0
        and total_transient_failures == expected_transient_failures
    )
    throughput = expected_cases / elapsed if elapsed else 0

    with ResultStore(database) as store:
        placeholders = ",".join("?" for _ in run_ids)
        stored_runs = store.connection.execute(
            f"SELECT COUNT(*) FROM runs WHERE run_id IN ({placeholders})", run_ids
        ).fetchone()[0]
        stored_results = store.connection.execute(
            f"SELECT COUNT(*) FROM case_results WHERE run_id IN ({placeholders})", run_ids
        ).fetchone()[0]
        stored_json = store.connection.execute(
            f"SELECT result_json FROM case_results WHERE run_id IN ({placeholders})", run_ids
        ).fetchall()
    integrity_passed = integrity_passed and stored_runs == process_count and stored_results == expected_cases

    latencies = []
    for row in stored_json:
        result = json.loads(row["result_json"])
        latency = trace_latency(result)
        if latency is not None:
            latencies.append(latency)
    p95_agent_latency = percentile(latencies, 0.95)
    if max_p95_agent_ms is None and min_throughput is None:
        slo_status = "not_configured"
    else:
        slo_status = "passed" if (
            (max_p95_agent_ms is None or (p95_agent_latency or 0) <= max_p95_agent_ms)
            and (min_throughput is None or throughput >= min_throughput)
        ) else "failed"

    return {
        "profile": profile,
        "processes": process_count,
        "workers_per_process": workers,
        "total_workers": process_count * workers,
        "cases_per_process": None if external_mode else cases_per_process,
        "input_mode": "external_adapter" if external_mode else "synthetic",
        "total_cases": expected_cases,
        "arrival_rate_per_process": arrival_rate_per_process,
        "elapsed_seconds": round(elapsed, 4),
        "throughput_cases_per_second": round(throughput, 2),
        "p95_agent_latency_ms": round(p95_agent_latency, 4) if p95_agent_latency else None,
        "peak_traced_memory_bytes": max(
            (summary.get("peak_traced_memory_bytes", 0) for summary in summaries),
            default=0,
        ),
        "stored_run_count": stored_runs,
        "stored_result_count": stored_results,
        "unique_case_count": total_unique,
        "hard_failures": total_hard_failures,
        "injected_transient_failures": total_transient_failures,
        "timed_out_process_ids": timed_out,
        "integrity_status": "passed" if integrity_passed else "failed",
        "slo_status": slo_status,
        "slo": {
            "max_p95_agent_ms": max_p95_agent_ms,
            "min_throughput_cases_per_second": min_throughput,
        },
        "process_results": [dict(summary) for summary in summaries],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a no-key multi-process scale-out benchmark against shared SQLite"
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--cases", type=Path)
    parser.add_argument("--profiles", default=",".join(PROFILES))
    parser.add_argument("--process-counts", type=parse_int_list, default=[1, 2, 4, 8])
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--cases-per-process", type=int, default=250)
    parser.add_argument("--arrival-rate-per-process", type=float, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=120)
    parser.add_argument("--max-p95-agent-ms", type=float)
    parser.add_argument("--min-throughput", type=float)
    args = parser.parse_args()

    if bool(args.adapter) != bool(args.cases):
        raise SystemExit("--adapter and --cases must be provided together")
    external_mode = bool(args.adapter and args.cases)
    profiles = ["external"] if external_mode else [
        item.strip() for item in args.profiles.split(",") if item.strip()
    ]
    if not profiles or (not external_mode and any(profile not in PROFILES for profile in profiles)):
        raise SystemExit(f"profiles must be selected from: {', '.join(PROFILES)}")
    if args.workers < 1 or (not external_mode and args.cases_per_process < 100):
        raise SystemExit("workers must be positive and synthetic cases-per-process must be at least 100")
    if args.arrival_rate_per_process < 0 or args.timeout_seconds <= 0:
        raise SystemExit("arrival rate must not be negative and timeout must be positive")
    total_case_count = (
        len(load_cases(args.cases, "scale-out")) if external_mode else 0
    )
    if external_mode and total_case_count < 1:
        raise SystemExit("external case file must not be empty")
    if external_mode and any(process_count > total_case_count for process_count in args.process_counts):
        raise SystemExit("external case count must cover every requested process count")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        topologies = [
            run_topology(
                root / f"{profile}-{process_count}.db",
                profile,
                process_count,
                args.workers,
                args.cases_per_process,
                args.arrival_rate_per_process,
                args.timeout_seconds,
                args.max_p95_agent_ms,
                args.min_throughput,
                total_case_count,
                str(args.adapter) if args.adapter else None,
                str(args.cases) if args.cases else None,
            )
            for profile in profiles
            for process_count in args.process_counts
        ]

    integrity_passed = all(item["integrity_status"] == "passed" for item in topologies)
    slo_configured = args.max_p95_agent_ms is not None or args.min_throughput is not None
    slo_passed = all(item["slo_status"] == "passed" for item in topologies)
    evidence = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "status": "passed" if integrity_passed else "failed",
        "claim_status": "passed" if slo_configured and slo_passed and integrity_passed else "not_claimable",
        "topology": "single-host multi-process runners sharing SQLite WAL",
        "input_mode": "external_adapter" if external_mode else "synthetic",
        "profiles": profiles,
        "process_counts": args.process_counts,
        "workers_per_process": args.workers,
        "cases_per_process": args.cases_per_process,
        "arrival_rate_per_process": args.arrival_rate_per_process,
        "topologies": topologies,
        "scope": "multi-process bounded case concurrency with configurable synthetic workload profiles",
        "limitations": [
            "This is a single-host scale-out test, not a multi-host distributed deployment test.",
            "Synthetic upstream behavior does not prove a specific business service capacity or quality.",
            "Performance claimability requires environment-specific SLO arguments.",
            "Peak memory uses Python tracemalloc and is not total process RSS.",
        ],
    }
    content = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    print(content)
    return 0 if integrity_passed else 1


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
