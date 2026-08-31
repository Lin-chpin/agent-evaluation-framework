from __future__ import annotations

import argparse
import json
import platform
import sys
import tempfile
import threading
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from agent_eval.engine import EvaluationEngine
from agent_eval.store import ResultStore
from verify_concurrency import build_adapter, build_cases


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a short single-machine soak verification")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--duration-seconds", type=float, default=10)
    parser.add_argument("--batch-cases", type=int, default=100)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args()
    if args.duration_seconds < 1 or args.batch_cases < 100 or args.workers < 1:
        raise SystemExit("soak verification requires duration >= 1, batch cases >= 100, and workers >= 1")

    started = time.perf_counter()
    deadline = started + args.duration_seconds
    initial_threads = threading.active_count()
    tracemalloc.start()
    samples: list[dict[str, int | float]] = []
    cycle = 0
    total_cases = 0
    failures: list[dict[str, int | str]] = []

    with tempfile.TemporaryDirectory() as directory:
        with ResultStore(Path(directory) / "soak.db") as store:
            adapter, _ = build_adapter()
            engine = EvaluationEngine(adapter, store, workers=args.workers, retries=1)
            while time.perf_counter() < deadline or cycle < 3:
                cycle += 1
                run_id = f"soak-{cycle}"
                result = engine.run_suite(build_cases(args.batch_cases), "soak", run_id=run_id)
                unique_cases = store.connection.execute(
                    "SELECT COUNT(DISTINCT case_id) FROM case_results WHERE run_id = ?",
                    (run_id,),
                ).fetchone()[0]
                total_cases += result["case_count"]
                if (
                    result["status"] != "passed"
                    or result["case_count"] != args.batch_cases
                    or unique_cases != args.batch_cases
                ):
                    failures.append(
                        {
                            "cycle": cycle,
                            "status": result["status"],
                            "case_count": result["case_count"],
                            "unique_case_count": unique_cases,
                        }
                    )
                current_memory, peak_memory = tracemalloc.get_traced_memory()
                samples.append(
                    {
                        "cycle": cycle,
                        "elapsed_seconds": round(time.perf_counter() - started, 3),
                        "current_traced_memory_bytes": current_memory,
                        "peak_traced_memory_bytes": peak_memory,
                        "active_threads": threading.active_count(),
                    }
                )

    final_threads = threading.active_count()
    passed = not failures and final_threads <= initial_threads
    evidence = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "status": "passed" if passed else "failed",
        "duration_seconds": round(time.perf_counter() - started, 3),
        "cycles": cycle,
        "batch_cases": args.batch_cases,
        "total_cases": total_cases,
        "workers": args.workers,
        "initial_threads": initial_threads,
        "final_threads": final_threads,
        "failures": failures,
        "memory": {
            "first_current_bytes": samples[0]["current_traced_memory_bytes"],
            "last_current_bytes": samples[-1]["current_traced_memory_bytes"],
            "peak_bytes": max(sample["peak_traced_memory_bytes"] for sample in samples),
        },
        "samples": samples,
        "scope": "short single-machine repeated-run integrity and worker-thread cleanup",
        "limitations": [
            "This short check does not replace a multi-hour soak test.",
            "Python tracemalloc does not measure total process RSS or external service resources.",
            "Real business capacity remains an integration-specific M4 acceptance task.",
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
