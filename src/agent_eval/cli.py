from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .auto_evolution import (
    AutoEvolutionLoop,
    load_auto_evolution_adapter,
)
from .engine import EvaluationEngine, load_adapter, load_cases
from .evolution import EvolutionEngine, load_candidate, load_policy
from .llm import OpenAICompatibleReviewer
from .model import EvolutionBudget, to_jsonable
from .review_samples import promote_review_record, write_review_record
from .reporting import (
    write_auto_evolution_artifacts,
    write_evolution_artifacts,
    write_run_artifacts,
)
from .store import ResultStore
from .workspace import TextArtifactWorkspace
from .test_selection import select_tests


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=Path(".agent-eval/evaluation.db"))
    parser.add_argument("--output", type=Path, default=Path(".agent-eval/runs"))
    parser.add_argument("--source", choices=("online", "offline"), default="online")
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--retries", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--collect-few-shot", action="store_true")


def _add_engine_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adapter", type=Path, required=True)
    _add_runtime_options(parser)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-eval")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="run one externally supplied suite")
    _add_engine_options(run)
    run.add_argument("--suite", required=True)
    run.add_argument("--cases", type=Path, required=True)
    run.add_argument("--run-id")
    run.add_argument("--resume", action="store_true")

    select = commands.add_parser(
        "select-tests", help="recommend test suites from the current Git diff"
    )
    select.add_argument("--repository", type=Path, default=Path("."))
    select.add_argument("--base", default="HEAD")
    select.add_argument("--ai-provider", choices=("none", "local", "remote"), default="none")
    select.add_argument("--ai-input", choices=("auto", "raw", "summary"), default="auto")
    select.add_argument("--confidence-threshold", type=float, default=0.7)
    select.add_argument("--output", type=Path)

    release = commands.add_parser(
        "release", help="run targeted regression, smoke, then optional full"
    )
    _add_engine_options(release)
    release.add_argument("--regression", type=Path, required=True)
    release.add_argument("--smoke", type=Path, required=True)
    release.add_argument("--full", type=Path)
    release.add_argument("--release-id")

    evolve = commands.add_parser(
        "evolve", help="compare a baseline and candidate across improvement, regression, and holdout"
    )
    _add_runtime_options(evolve)
    evolve.add_argument("--baseline-adapter", type=Path, required=True)
    evolve.add_argument("--candidate-adapter", type=Path, required=True)
    evolve.add_argument("--candidate", type=Path, required=True)
    evolve.add_argument("--policy", type=Path)
    evolve.add_argument("--improvement", type=Path, required=True)
    evolve.add_argument("--regression", type=Path, required=True)
    evolve.add_argument("--holdout", type=Path, required=True)
    evolve.add_argument("--experiment-id")

    auto_evolve = commands.add_parser(
        "evolve-auto",
        help="diagnose, generate, apply, and evaluate text candidates in a sandbox",
    )
    _add_runtime_options(auto_evolve)
    auto_evolve.add_argument("--auto-adapter", type=Path, required=True)
    auto_evolve.add_argument("--policy", type=Path)
    auto_evolve.add_argument("--improvement", type=Path, required=True)
    auto_evolve.add_argument("--regression", type=Path, required=True)
    auto_evolve.add_argument("--holdout", type=Path, required=True)
    auto_evolve.add_argument("--loop-id")
    auto_evolve.add_argument("--workspace", type=Path, default=Path(".agent-eval/workspaces"))
    auto_evolve.add_argument("--max-rounds", type=int, default=3)
    auto_evolve.add_argument("--max-candidates-per-round", type=int, default=3)
    auto_evolve.add_argument("--max-elapsed-seconds", type=float)
    auto_evolve.add_argument("--max-evolver-calls", type=int)
    auto_evolve.add_argument("--resume", action="store_true")

    review = commands.add_parser("review", help="store the human final conclusion")
    review.add_argument("--db", type=Path, default=Path(".agent-eval/evaluation.db"))
    review.add_argument("--run-id", required=True)
    review.add_argument("--case-id", required=True)
    review.add_argument(
        "--decision",
        required=True,
        choices=("confirmed_badcase", "false_positive", "accepted", "needs_follow_up"),
    )
    review.add_argument("--conclusion", required=True)

    export = commands.add_parser(
        "export", help="export human-confirmed regression or few-shot candidates"
    )
    export.add_argument("--db", type=Path, default=Path(".agent-eval/evaluation.db"))
    export.add_argument("--kind", required=True, choices=("regression", "few-shot"))
    export.add_argument("--output", type=Path, required=True)

    promote = commands.add_parser(
        "promote-review", help="adjudicate one evaluator-Skill review sample"
    )
    promote.add_argument("--input", type=Path, required=True)
    promote.add_argument("--output", type=Path, required=True)
    promote.add_argument(
        "--outcome",
        required=True,
        choices=("CORRECT", "PARTIALLY_CORRECT", "INCORRECT", "UNRESOLVED"),
    )
    promote.add_argument(
        "--role", required=True, choices=("improvement", "regression", "holdout", "pending")
    )
    promote.add_argument("--conclusion", required=True)
    promote.add_argument("--reviewer", required=True)
    promote.add_argument("--reviewed-at")
    return parser


def _reviewer(enabled: bool) -> OpenAICompatibleReviewer | None:
    if not enabled:
        return None
    reviewer = OpenAICompatibleReviewer.from_environment()
    if reviewer is None:
        raise SystemExit("--use-llm requires AGENT_EVAL_MODEL")
    return reviewer


def _engine_for_adapter(
    args: argparse.Namespace, store: ResultStore, adapter_path: Path
) -> EvaluationEngine:
    return EvaluationEngine(
        load_adapter(adapter_path),
        store,
        reviewer=_reviewer(args.use_llm),
        workers=args.workers,
        retries=args.retries,
        timeout_seconds=args.timeout,
        collect_few_shot=args.collect_few_shot,
    )


def _engine(args: argparse.Namespace, store: ResultStore) -> EvaluationEngine:
    return _engine_for_adapter(args, store, args.adapter)


def _run(args: argparse.Namespace) -> int:
    with ResultStore(args.db) as store:
        engine = _engine(args, store)
        cases = load_cases(args.cases, args.suite)
        summary = engine.run_suite(
            cases,
            args.suite,
            source=args.source,
            run_id=args.run_id,
            resume=args.resume,
        )
    output = args.output / summary["run_id"]
    write_run_artifacts(summary, output)
    print(json.dumps({key: value for key, value in summary.items() if key != "results"}, ensure_ascii=False))
    print(f"report: {output / 'report.md'}")
    return 1 if summary["hard_failures"] else 0


def _select_tests(args: argparse.Namespace) -> int:
    reviewer = None
    if args.ai_provider != "none":
        reviewer = OpenAICompatibleReviewer.from_environment()
        if reviewer is None:
            raise SystemExit("AI selection requires AGENT_EVAL_MODEL")
    selection = select_tests(
        args.repository,
        args.base,
        ai_provider=args.ai_provider,
        ai_input=args.ai_input,
        reviewer=reviewer,
        confidence_threshold=args.confidence_threshold,
    )
    content = json.dumps(to_jsonable(selection), ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    print(content)
    return 2 if selection.human_review_required else 0


def _release(args: argparse.Namespace) -> int:
    suite_paths = [("regression", args.regression), ("smoke", args.smoke)]
    if args.full:
        suite_paths.append(("full", args.full))
    suites = [(name, load_cases(path, name)) for name, path in suite_paths]

    with ResultStore(args.db) as store:
        release = _engine(args, store).run_release(
            suites,
            source=args.source,
            release_id=args.release_id,
        )

    output = args.output / release["release_id"]
    output.mkdir(parents=True, exist_ok=True)
    for stage in release["stages"]:
        write_run_artifacts(stage, output / stage["suite"])
    (output / "release.json").write_text(
        json.dumps(release, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({"release_id": release["release_id"], "status": release["status"]}, ensure_ascii=False))
    print(f"release: {output / 'release.json'}")
    return 1 if release["status"] == "failed" else 0


def _evolve(args: argparse.Namespace) -> int:
    datasets = {
        "improvement": load_cases(args.improvement, "improvement"),
        "regression": load_cases(args.regression, "regression"),
        "holdout": load_cases(args.holdout, "holdout"),
    }
    change = load_candidate(args.candidate)
    with ResultStore(args.db) as store:
        result = EvolutionEngine(
            _engine_for_adapter(args, store, args.baseline_adapter),
            _engine_for_adapter(args, store, args.candidate_adapter),
            load_policy(args.policy),
        ).run(
            change,
            datasets,
            source=args.source,
            experiment_id=args.experiment_id,
        )

    output = args.output / result["experiment_id"]
    write_evolution_artifacts(result, output)
    for role in ("improvement", "regression", "holdout"):
        write_run_artifacts(result["baseline_runs"][role], output / "baseline" / role)
        write_run_artifacts(result["candidate_runs"][role], output / "candidate" / role)
    print(
        json.dumps(
            {
                "experiment_id": result["experiment_id"],
                "candidate_id": change.candidate_id,
                "decision": result["decision"],
            },
            ensure_ascii=False,
        )
    )
    print(f"evolution: {output / 'evolution_report.md'}")
    return 0 if result["decision"] == "accept" else 1


def _auto_evolve(args: argparse.Namespace) -> int:
    datasets = {
        "improvement": load_cases(args.improvement, "improvement"),
        "regression": load_cases(args.regression, "regression"),
        "holdout": load_cases(args.holdout, "holdout"),
    }
    adapter = load_auto_evolution_adapter(args.auto_adapter)
    with ResultStore(args.db) as store:
        result = AutoEvolutionLoop(
            store,
            TextArtifactWorkspace(args.workspace),
            policy=load_policy(args.policy),
            workers=args.workers,
            retries=args.retries,
            timeout_seconds=args.timeout,
        ).run(
            adapter,
            datasets,
            EvolutionBudget(
                max_rounds=args.max_rounds,
                max_candidates_per_round=args.max_candidates_per_round,
                max_elapsed_seconds=args.max_elapsed_seconds,
                max_evolver_calls=args.max_evolver_calls,
            ),
            loop_id=args.loop_id,
            source=args.source,
            resume=args.resume,
        )

    output = args.output / result["loop_id"]
    write_auto_evolution_artifacts(result, output)
    for round_result in result["rounds"]:
        for candidate in round_result["candidates"]:
            evaluation = candidate["evaluation"]
            candidate_output = output / "rounds" / str(round_result["round"]) / evaluation[
                "candidate"
            ]["candidate_id"]
            write_evolution_artifacts(evaluation, candidate_output)
    print(
        json.dumps(
            {
                "loop_id": result["loop_id"],
                "status": result["status"],
                "current_version": result["current_version"],
            },
            ensure_ascii=False,
        )
    )
    print(f"auto evolution: {output / 'auto_evolution_report.md'}")
    return 0 if result["status"] == "completed" else 1


def _review(args: argparse.Namespace) -> int:
    with ResultStore(args.db) as store:
        store.save_review(
            args.run_id,
            args.case_id,
            args.decision,
            args.conclusion,
        )
    print("human conclusion saved")
    return 0


def _export(args: argparse.Namespace) -> int:
    decision = "confirmed_badcase" if args.kind == "regression" else "accepted"
    with ResultStore(args.db) as store:
        results = store.list_reviewed_results(decision)
    rows: list[dict[str, Any]] = []
    for result in results:
        if args.kind == "regression":
            case = result["case"]
            rows.append(
                {
                    "id": case["case_id"],
                    "scenario": case["scenario"],
                    "input": case["payload"],
                    "expected": case["expected"],
                    "metadata": case["metadata"],
                    "human_final_conclusion": result["human_final_conclusion"],
                }
            )
        elif result.get("few_shot_candidate"):
            rows.append(
                {
                    **result["few_shot_candidate"],
                    "status": "human_accepted",
                    "human_final_conclusion": result["human_final_conclusion"],
                }
            )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    print(f"exported {len(rows)} records to {args.output}")
    return 0


def _promote_review(args: argparse.Namespace) -> int:
    content = args.input.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError("review input is empty")
    record = json.loads(content)
    if args.output.exists():
        record_id = str(record.get("id") or record.get("case_id"))
        for line in args.output.read_text(encoding="utf-8").splitlines():
            existing = json.loads(line)
            if str(existing.get("id") or existing.get("case_id")) == record_id:
                record = existing
                break
    promoted = promote_review_record(
        record,
        outcome=args.outcome,
        conclusion=args.conclusion,
        role=args.role,
        reviewer=args.reviewer,
        reviewed_at=args.reviewed_at,
    )
    write_review_record(args.output, promoted)
    print(f"promoted {promoted.get('id') or promoted.get('case_id')} to {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "run":
        return _run(args)
    if args.command == "select-tests":
        return _select_tests(args)
    if args.command == "release":
        return _release(args)
    if args.command == "evolve":
        return _evolve(args)
    if args.command == "evolve-auto":
        return _auto_evolve(args)
    if args.command == "review":
        return _review(args)
    if args.command == "promote-review":
        return _promote_review(args)
    return _export(args)


if __name__ == "__main__":
    raise SystemExit(main())
