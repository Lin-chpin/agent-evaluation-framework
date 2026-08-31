from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_eval.evaluator_metrics import classification_metrics


def framework_version() -> str:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(metadata["project"]["version"])


def source_revision() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None


def source_is_dirty() -> bool | None:
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return bool(completed.stdout.strip()) if completed.returncode == 0 else None


def run(
    command: list[str],
    environment: dict[str, str],
    expected_exit_codes: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in expected_exit_codes:
        sys.stderr.write(completed.stdout)
        sys.stderr.write(completed.stderr)
        raise SystemExit(completed.returncode)
    return completed


def dataset_evidence(path: Path) -> dict:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return {
        "case_count": len(rows),
        "human_reviewed_cases": sum(
            bool(row.get("metadata", {}).get("human_reviewed")) for row in rows
        ),
        "simulated_review_cases": sum(
            row.get("metadata", {}).get("review_status") == "simulated" for row in rows
        ),
        "source_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest().upper(),
    }


def evolution_evidence(
    adapter: str,
    loop_id: str,
    temporary: Path,
    environment: dict[str, str],
    dataset_prefix: str = "evolution",
    policy: str = "examples/evolution.policy.json",
    expect_accept: bool = True,
) -> dict:
    dataset_paths = {
        role: ROOT / "examples" / f"{dataset_prefix}.{role}.jsonl"
        for role in ("improvement", "regression", "holdout")
    }
    output = temporary / f"{loop_id}-runs"
    workspace = temporary / f"{loop_id}-workspaces"
    run(
        [
            sys.executable,
            "-m",
            "agent_eval",
            "evolve-auto",
            "--auto-adapter",
            adapter,
            "--policy",
            policy,
            "--improvement",
            f"examples/{dataset_prefix}.improvement.jsonl",
            "--regression",
            f"examples/{dataset_prefix}.regression.jsonl",
            "--holdout",
            f"examples/{dataset_prefix}.holdout.jsonl",
            "--max-rounds",
            "1",
            "--max-candidates-per-round",
            "2",
            "--max-elapsed-seconds",
            "60",
            "--max-evolver-calls",
            "2",
            "--loop-id",
            loop_id,
            "--workspace",
            str(workspace),
            "--output",
            str(output),
            "--db",
            str(temporary / f"{loop_id}.db"),
        ],
        environment,
        (0,) if expect_accept else (0, 1),
    )
    result = json.loads((output / loop_id / "auto_evolution.json").read_text(encoding="utf-8"))
    candidates = [
        candidate
        for round_result in result["rounds"]
        for candidate in round_result["candidates"]
    ]
    accepted = next(
        (item for item in candidates if item["evaluation"]["decision"] == "accept"),
        None,
    )
    if expect_accept and accepted is None:
        raise RuntimeError(f"{loop_id} did not accept a candidate")
    selected = accepted or candidates[-1]
    comparisons = selected["evaluation"]["comparisons"]
    evidence = {
        "status": result["status"],
        "target_type": result["target_type"],
        "target_id": result["target_id"],
        "initial_version": result["initial_version"],
        "current_version": result["current_version"],
        "candidate_decisions": [item["evaluation"]["decision"] for item in candidates],
        "accepted_candidate": accepted is not None,
        "evolver_calls": result["usage"]["evolver_calls"],
        "evolver_retries": result["usage"]["evolver_retries"],
        "datasets": {
            role: dataset_evidence(path)
            for role, path in dataset_paths.items()
        },
        "normalized_case_manifest": result["datasets"],
        "hard_pass_rates": {
            role: {
                "baseline": comparisons[role]["baseline"]["hard_pass_rate"],
                "candidate": comparisons[role]["candidate"]["hard_pass_rate"],
            }
            for role in ("improvement", "regression", "holdout")
        },
    }
    if result["target_type"] == "evaluator_skill":
        evidence["classification_metrics"] = {
            role: {
                "baseline": classification_metrics(
                    selected["evaluation"]["baseline_runs"][role]["results"]
                ),
                "candidate": classification_metrics(
                    selected["evaluation"]["candidate_runs"][role]["results"]
                ),
            }
            for role in ("improvement", "regression", "holdout")
        }
    return evidence


def main() -> int:
    if sys.version_info < (3, 11):
        raise SystemExit("evidence verification requires Python 3.11 or newer")
    parser = argparse.ArgumentParser(description="Reproduce the public no-key evidence suite")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(ROOT / "src"), environment.get("PYTHONPATH", "")))
    )
    tests = run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        environment,
    )
    match = re.search(r"Ran (\d+) tests?", tests.stdout + tests.stderr)
    if match is None:
        raise RuntimeError("could not read unittest count")

    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        evidence = {
            "schema_version": 2,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "python": sys.version.split()[0],
            "framework": {
                "name": "agent-evaluation-framework",
                "version": framework_version(),
                "source_revision": source_revision(),
                "source_is_dirty": source_is_dirty(),
            },
            "platform": {
                "system": platform.system(),
                "release": platform.release(),
                "machine": platform.machine(),
            },
            "reproduction": {
                "command": "python scripts/verify_evidence.py --output evidence/verified-results.json"
            },
            "tests": {"status": "passed", "count": int(match.group(1))},
            "text_evolution": evolution_evidence(
                "examples/auto_evolution_adapter.py",
                "evidence-text",
                temporary,
                environment,
            ),
            "code_evolution": evolution_evidence(
                "examples/code_auto_evolution_adapter.py",
                "evidence-code",
                temporary,
                environment,
            ),
            "evaluator_skill_evolution": evolution_evidence(
                "examples/evaluator_skill_auto_evolution_adapter.py",
                "evidence-evaluator-skill",
                temporary,
                environment,
                dataset_prefix="evaluator_skill",
            ),
            "evaluator_skill_human_evolution": evolution_evidence(
                "examples/evaluator_skill_human_auto_evolution_adapter.py",
                "evidence-evaluator-skill-human",
                temporary,
                environment,
                dataset_prefix="evaluator_skill_human",
                policy="examples/evaluator_skill_human.policy.json",
                expect_accept=False,
            ),
        }

    content = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
