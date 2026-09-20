from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_eval import EvalCase, NormalizedTrace, ProjectAdapter, Rule, RunContext, TraceEvent
from agent_eval.engine import EvaluationEngine
from agent_eval.store import ResultStore


PROFILES = {
    "short_io": {"base_delay_ms": 1.0, "trace_bytes": 0},
    "long_io": {"base_delay_ms": 20.0, "trace_bytes": 0},
    "trace_heavy": {"base_delay_ms": 5.0, "trace_bytes": 8192},
    "mixed_io": {"base_delay_ms": 1.0, "trace_bytes": 1024},
}


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "runner"


class StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    failed_requests: set[str] = set()
    injected_failures: int = 0
    state_lock = threading.Lock()

    def log_message(self, *_: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "passed"})
        elif self.path == "/stats":
            with self.state_lock:
                injected = type(self).injected_failures
            self._send_json(200, {"injected_transient_failures": injected})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/invoke":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            request = json.loads(self.rfile.read(length))
            profile = str(request["profile"])
            settings = PROFILES[profile]
            case_id = str(request["case_id"])
            index = int(request["index"])
            attempt = int(request["attempt"])
            run_id = str(request["run_id"])
        except (KeyError, ValueError, TypeError, json.JSONDecodeError):
            self._send_json(400, {"error": "invalid request"})
            return

        failure_key = f"{run_id}:{case_id}"
        if index % 100 == 0 and attempt == 0:
            with self.state_lock:
                first_attempt = failure_key not in self.failed_requests
            self.failed_requests.add(failure_key)
            if first_attempt:
                with self.state_lock:
                    type(self).injected_failures += 1
                self._send_json(503, {"error": "injected transient failure"})
                return

        delay_ms = float(settings["base_delay_ms"])
        trace_bytes = int(settings["trace_bytes"])
        if profile == "mixed_io":
            delay_ms *= 1 + (index % 10)
            trace_bytes *= 1 + (index % 8)
        started = time.perf_counter()
        time.sleep(delay_ms / 1000)
        self._send_json(
            200,
            {
                "trace_id": f"{run_id}-{case_id}",
                "route": "OK",
                "latency_ms": (time.perf_counter() - started) * 1000,
                "trace_payload": "x" * trace_bytes,
            },
        )


class StubServer(ThreadingHTTPServer):
    request_queue_size = 128


def build_http_adapter(stub_url: str, profile: str) -> tuple[ProjectAdapter, dict[str, int]]:
    counters = {"transient_failures": 0}
    counter_lock = threading.Lock()

    def call_agent(case: EvalCase, context: RunContext) -> dict[str, Any]:
        request = Request(
            stub_url,
            data=json.dumps(
                {
                    "case_id": case.case_id,
                    "index": case.payload["index"],
                    "profile": profile,
                    "run_id": context.run_id,
                    "attempt": context.attempt,
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=max(1.0, context.timeout_seconds)) as response:
                return json.loads(response.read())
        except HTTPError as error:
            if error.code == 503 and context.attempt == 0:
                with counter_lock:
                    counters["transient_failures"] += 1
            raise RuntimeError(f"agent stub HTTP {error.code}") from error
        except (OSError, URLError, json.JSONDecodeError) as error:
            raise RuntimeError(f"agent stub request failed: {error}") from error

    def read_trace(handle: dict[str, Any], _: EvalCase) -> NormalizedTrace:
        latency = float(handle["latency_ms"])
        payload = str(handle["trace_payload"])
        return NormalizedTrace(
            str(handle["trace_id"]),
            {"route": handle["route"]},
            (TraceEvent("NetworkAgentStub", "invoke", duration_ms=latency),),
            {"route": handle["route"], "latency_ms": latency, "trace_bytes": len(payload)},
            raw={"payload": payload} if payload else {},
        )

    return (
        ProjectAdapter(
            f"distributed-http-stub-{profile}",
            call_agent,
            read_trace,
            (Rule("route", "fields.route", expected="route"),),
            (),
        ),
        counters,
    )


def build_cases(runner_id: str, count: int) -> list[EvalCase]:
    return [
        EvalCase(
            f"{runner_id}-{index:05d}",
            {"index": index, "runner_id": runner_id},
            {"route": "OK"},
            suite="distributed",
        )
        for index in range(count)
    ]


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def run_runner(args: argparse.Namespace) -> int:
    runner_id = safe_name(args.runner_id or os.getenv("RUNNER_ID") or os.getenv("HOSTNAME", "runner"))
    profile = args.profile or os.getenv("PROFILE", "mixed_io")
    workers = args.workers or int(os.getenv("WORKERS", "8"))
    cases_per_runner = args.cases_per_runner or int(os.getenv("CASES_PER_RUNNER", "250"))
    results_dir = Path(args.results_dir or os.getenv("RESULTS_DIR", "/results"))
    stub_url = args.stub_url or os.getenv("AGENT_STUB_URL", "http://agent-stub:8080/invoke")
    output_path = results_dir / f"{runner_id}.json"
    started = time.time()
    try:
        adapter, counters = build_http_adapter(stub_url, profile)
        cases = build_cases(runner_id, cases_per_runner)
        database = results_dir / "db" / f"{runner_id}.db"
        with ResultStore(database) as store:
            result = EvaluationEngine(adapter, store, workers=workers, retries=1).run_suite(
                cases, "distributed", run_id=f"distributed-{profile}-{runner_id}"
            )
            rows = store.connection.execute(
                "SELECT result_json FROM case_results WHERE run_id = ?",
                (result["run_id"],),
            ).fetchall()
        stored = [json.loads(row[0]) for row in rows]
        latencies = [
            float(item["trace"]["fields"]["latency_ms"])
            for item in stored
            if item.get("trace")
            and item["trace"].get("fields", {}).get("latency_ms") is not None
        ]
        payload = {
            "runner_id": runner_id,
            "profile": profile,
            "workers": workers,
            "case_count": result["case_count"],
            "stored_result_count": len(stored),
            "hard_failures": result["hard_failures"],
            "injected_transient_failures": counters["transient_failures"],
            "status": result["status"],
            "case_ids": [item["case"]["case_id"] for item in stored],
            "latencies_ms": latencies,
            "started_at": started,
            "finished_at": time.time(),
            "storage": "runner-local-sqlite",
        }
        write_json(output_path, payload)
        return 0 if result["status"] == "passed" else 1
    except Exception as error:
        write_json(
            output_path,
            {
                "runner_id": runner_id,
                "profile": profile,
                "status": "error",
                "error": f"{type(error).__name__}: {error}",
                "started_at": started,
                "finished_at": time.time(),
            },
        )
        return 1


def run_collector(args: argparse.Namespace) -> int:
    results_dir = Path(args.results_dir or os.getenv("RESULTS_DIR", "/results"))
    expected_runners = args.expected_runners or int(os.getenv("EXPECTED_RUNNERS", "8"))
    cases_per_runner = args.cases_per_runner or int(os.getenv("CASES_PER_RUNNER", "250"))
    max_p95 = args.max_p95_agent_ms or float(os.getenv("MAX_P95_AGENT_MS", "25"))
    min_throughput = args.min_throughput or float(os.getenv("MIN_THROUGHPUT", "300"))
    output_path = Path(args.output or os.getenv("OUTPUT", str(results_dir / "aggregate.json")))
    records = []
    for path in sorted(results_dir.glob("*.json")):
        if path.resolve() == output_path.resolve():
            continue
        try:
            records.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue

    case_ids = [case_id for record in records for case_id in record.get("case_ids", [])]
    latencies = [latency for record in records for latency in record.get("latencies_ms", [])]
    total_cases = expected_runners * cases_per_runner
    started = min((record.get("started_at", time.time()) for record in records), default=time.time())
    finished = max((record.get("finished_at", started) for record in records), default=started)
    elapsed = max(0.001, finished - started)
    p95 = percentile(latencies, 0.95)
    transient_expected = expected_runners * (math.floor((cases_per_runner - 1) / 100) + 1)
    observed_transient_failures = sum(
        record.get("injected_transient_failures", 0) for record in records
    )
    injected_transient_failures = observed_transient_failures
    if "INJECTED_TRANSIENT_FAILURES" in os.environ:
        try:
            injected_transient_failures = int(os.environ["INJECTED_TRANSIENT_FAILURES"])
        except ValueError:
            injected_transient_failures = -1
    integrity = (
        len(records) == expected_runners
        and all(record.get("status") == "passed" for record in records)
        and sum(record.get("case_count", 0) for record in records) == total_cases
        and sum(record.get("stored_result_count", 0) for record in records) == total_cases
        and len(case_ids) == total_cases
        and len(set(case_ids)) == total_cases
        and sum(record.get("hard_failures", 0) for record in records) == 0
        and injected_transient_failures == transient_expected
    )
    throughput = total_cases / elapsed
    slo = integrity and (p95 is not None and p95 <= max_p95) and throughput >= min_throughput
    payload = {
        "schema_version": 1,
        "generated_at": time.time(),
        "status": "passed" if integrity and slo else "failed",
        "claim_status": "not_claimable",
        "topology": os.getenv(
            "TOPOLOGY", "single-host independent runner processes with network Agent stub"
        ),
        "runner_count": expected_runners,
        "workers_per_runner": records[0].get("workers") if records else None,
        "total_workers": sum(record.get("workers", 0) for record in records),
        "profile": records[0].get("profile") if records else None,
        "total_cases": total_cases,
        "throughput_cases_per_second": round(throughput, 2),
        "p95_agent_latency_ms": round(p95, 4) if p95 is not None else None,
        "stored_result_count": sum(record.get("stored_result_count", 0) for record in records),
        "unique_case_count": len(set(case_ids)),
        "hard_failures": sum(record.get("hard_failures", 0) for record in records),
        "injected_transient_failures": injected_transient_failures,
        "observed_transient_failures": observed_transient_failures,
        "integrity_status": "passed" if integrity else "failed",
        "slo_status": "passed" if slo else "failed",
        "slo": {"max_p95_agent_ms": max_p95, "min_throughput_cases_per_second": min_throughput},
        "runner_results": records,
        "limitations": [
            "All containers run on one physical host.",
            "Each runner uses its own SQLite database; this does not prove shared distributed persistence.",
            "The network Agent stub is synthetic and does not prove business quality or upstream capacity.",
            "A real multi-host run and production-grade shared store are required for a production claim.",
        ],
    }
    write_json(output_path, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "passed" else 1


def run_stub(args: argparse.Namespace) -> int:
    server = StubServer((args.host, args.port), StubHandler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def run_self_check() -> int:
    server = StubServer(("127.0.0.1", 0), StubHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory() as directory:
            adapter, counters = build_http_adapter(
                f"http://127.0.0.1:{server.server_port}/invoke", "short_io"
            )
            with ResultStore(Path(directory) / "check.db") as store:
                result = EvaluationEngine(adapter, store, workers=2, retries=1).run_suite(
                    build_cases("self-check", 2), "distributed", run_id="self-check"
                )
            if result["status"] != "passed":
                print(json.dumps(result, ensure_ascii=False, indent=2))
            assert result["status"] == "passed"
            assert counters["transient_failures"] == 1
        print("distributed compose self-check passed")
        return 0
    finally:
        server.shutdown()
        thread.join(5)


def run_compose(args: argparse.Namespace) -> int:
    if shutil.which("docker") is None:
        print("docker engine not found; install Docker Desktop or run --mode self-check", file=sys.stderr)
        return 2
    results_dir = Path(args.results_dir or ROOT / "tmp" / "distributed-compose" / f"{args.profile}-{int(time.time())}")
    results_dir.mkdir(parents=True, exist_ok=True)
    compose = ["docker", "compose", "-f", str(ROOT / "compose.yaml")]
    environment = os.environ.copy()
    environment.update(
        {
            "DISTRIBUTED_RESULTS_DIR": str(results_dir.resolve()),
            "DISTRIBUTED_RESULTS_VOLUME": f"agent-eval-results-{int(time.time() * 1000)}",
            "PROFILE": args.profile,
            "WORKERS": str(args.workers),
            "CASES_PER_RUNNER": str(args.cases_per_runner),
            "EXPECTED_RUNNERS": str(args.runners),
            "MAX_P95_AGENT_MS": str(args.max_p95_agent_ms),
            "MIN_THROUGHPUT": str(args.min_throughput),
        }
    )
    subprocess.run([*compose, "up", "-d", "agent-stub"], cwd=ROOT, env=environment, check=True)
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                with urlopen("http://127.0.0.1:18080/health", timeout=1):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            raise RuntimeError("agent stub did not become healthy")

        def launch(index: int) -> int:
            completed = subprocess.run(
                [
                    *compose,
                    "run",
                    "--rm",
                    "--no-deps",
                    "-e",
                    f"RUNNER_ID=runner-{index}",
                    "runner",
                    "python",
                    "scripts/verify_distributed_compose.py",
                    "--mode",
                    "runner",
                    "--profile",
                    args.profile,
                    "--workers",
                    str(args.workers),
                    "--cases-per-runner",
                    str(args.cases_per_runner),
                ],
                cwd=ROOT,
                env=environment,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
            )
            if completed.returncode:
                print((completed.stderr or "")[-4000:], file=sys.stderr)
            return completed.returncode

        with ThreadPoolExecutor(max_workers=args.runners) as executor:
            return_codes = list(executor.map(launch, range(args.runners)))
        if any(return_codes):
            print(f"runner failures: {return_codes}", file=sys.stderr)
        injected_transient_failures = None
        try:
            with urlopen("http://127.0.0.1:18080/stats", timeout=2) as response:
                stats = json.loads(response.read())
            injected_transient_failures = int(stats["injected_transient_failures"])
        except (OSError, ValueError, KeyError):
            pass
        collector_command = [*compose, "run", "--rm", "--no-deps"]
        if injected_transient_failures is not None:
            collector_command.extend(
                ["-e", f"INJECTED_TRANSIENT_FAILURES={injected_transient_failures}"]
            )
        collector_command.append("collector")
        collector = subprocess.run(
            collector_command,
            cwd=ROOT,
            env=environment,
            text=True,
        )
        aggregate = results_dir / "aggregate.json"
        if args.output and aggregate.exists():
            shutil.copy2(aggregate, args.output)
        print(f"evidence: {aggregate}")
        return collector.returncode
    finally:
        subprocess.run([*compose, "down", "--remove-orphans"], cwd=ROOT, env=environment, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a logical distributed Compose benchmark")
    parser.add_argument("--mode", choices=("compose", "stub", "runner", "collect", "self-check"), default="compose")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--runner-id")
    parser.add_argument("--runners", type=int, default=8)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--cases-per-runner", type=int, default=250)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="mixed_io")
    parser.add_argument("--stub-url")
    parser.add_argument("--results-dir")
    parser.add_argument("--expected-runners", type=int)
    parser.add_argument("--output")
    parser.add_argument("--max-p95-agent-ms", type=float, default=25)
    parser.add_argument("--min-throughput", type=float, default=300)
    args = parser.parse_args()
    if args.mode == "stub":
        return run_stub(args)
    if args.mode == "runner":
        return run_runner(args)
    if args.mode == "collect":
        return run_collector(args)
    if args.mode == "self-check":
        return run_self_check()
    return run_compose(args)


if __name__ == "__main__":
    raise SystemExit(main())
