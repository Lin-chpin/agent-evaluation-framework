from __future__ import annotations

import json
from collections import Counter
from math import sqrt
from pathlib import Path
from typing import Any, Mapping


def _failed_checks(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [check for check in result["checks"] if not check["passed"]]


def _wilson(successes: int, total: int) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    z = 1.959963984540054
    rate = successes / total
    denominator = 1 + z * z / total
    centre = rate + z * z / (2 * total)
    margin = z * sqrt((rate * (1 - rate) + z * z / (4 * total)) / total)
    return (centre - margin) / denominator, (centre + margin) / denominator


def write_run_artifacts(summary: Mapping[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    module_counts: Counter[str] = Counter()
    scenario_stats: dict[str, dict[str, int]] = {}
    target_scenario_stats: dict[str, dict[str, int]] = {}
    review_rows: list[dict[str, Any]] = []
    few_shot_rows: list[Mapping[str, Any]] = []
    failures: list[str] = []

    for result in summary["results"]:
        case = result["case"]
        scenario = case["scenario"]
        trace = result.get("trace") or {}
        target = f"{trace.get('target_type', 'unknown')}:{trace.get('target_id', 'unknown')}"
        for stats in (
            scenario_stats.setdefault(scenario, {"cases": 0, "hard_passes": 0, "soft_warnings": 0}),
            target_scenario_stats.setdefault(
                f"{target}|{scenario}", {"cases": 0, "hard_passes": 0, "soft_warnings": 0}
            ),
        ):
            stats["cases"] += 1
            stats["hard_passes"] += int(result["hard_pass"])
            stats["soft_warnings"] += result["soft_warning_count"]
        failed = _failed_checks(result)
        if failed:
            for check in failed:
                module_counts.update(check.get("suspected_modules", []))
            failures.append(
                f"- `{case['case_id']}` ({case['scenario']}): "
                + "; ".join(f"{check['layer']}/{check['name']}" for check in failed)
            )
            review_rows.append(
                {
                    "run_id": summary["run_id"],
                    "case_id": case["case_id"],
                    "auto_result": "hard_failure" if not result["hard_pass"] else "review_candidate",
                    "failed_checks": failed,
                    "llm_review": result.get("llm_review", {}),
                    "human_decision": "",
                    "human_final_conclusion": "",
                }
            )
        if result.get("few_shot_candidate"):
            few_shot_rows.append(result["few_shot_candidate"])

    report = [
        f"# Agent Evaluation Report — {summary['run_id']}",
        "",
        f"- Suite: `{summary['suite']}`",
        f"- Source: `{summary['source']}`",
        f"- Status: **{summary['status']}**",
        f"- Cases: {summary['case_count']}",
        f"- Hard failures: {summary['hard_failures']}",
        f"- Soft/review warnings: {summary['soft_warnings']}",
        "",
        "## Suspected modules",
        "",
    ]
    report.extend(
        [f"- `{module}`: {count}" for module, count in module_counts.most_common()]
        or ["- None"]
    )
    scenario_output: dict[str, Any] = {"scenarios": {}, "target_scenarios": {}}
    report.extend(["", "## Scenario statistics", ""])
    for scenario, stats in sorted(scenario_stats.items()):
        lower, upper = _wilson(stats["hard_passes"], stats["cases"])
        rate = stats["hard_passes"] / stats["cases"]
        scenario_output["scenarios"][scenario] = {
            **stats,
            "hard_pass_rate": rate,
            "confidence_95": [lower, upper],
        }
        report.append(
            f"- `{scenario}`: {stats['hard_passes']}/{stats['cases']} "
            f"({rate:.1%}, 95% CI {lower:.1%}–{upper:.1%}), "
            f"soft warnings {stats['soft_warnings']}"
        )
    for key, stats in sorted(target_scenario_stats.items()):
        lower, upper = _wilson(stats["hard_passes"], stats["cases"])
        scenario_output["target_scenarios"][key] = {
            **stats,
            "hard_pass_rate": stats["hard_passes"] / stats["cases"],
            "confidence_95": [lower, upper],
        }
    report.extend(["", "## Failed or review-candidate cases", ""])
    report.extend(failures or ["- None"])
    report.extend(
        [
            "",
            "> LLM analysis and module suggestions are non-authoritative candidates. "
            "Human final conclusions must be stored separately.",
            "",
        ]
    )
    (output_dir / "report.md").write_text("\n".join(report), encoding="utf-8")
    (output_dir / "scenario_stats.json").write_text(
        json.dumps(scenario_output, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    _write_jsonl(output_dir / "review_queue.jsonl", review_rows)
    _write_jsonl(output_dir / "few_shot_candidates.jsonl", few_shot_rows)


def write_evolution_artifacts(result: Mapping[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "evolution.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    candidate = result["candidate"]
    report = [
        f"# Evolution Report — {result['experiment_id']}",
        "",
        f"- Decision: **{result['decision']}**",
        f"- Candidate: `{candidate['candidate_id']}`",
        f"- Target: `{candidate['target_type']}:{candidate['target_id']}`",
        f"- Version: `{candidate['baseline_version']}` → `{candidate['candidate_version']}`",
        f"- Change type: `{candidate['change_type']}`",
        "",
        "## Version comparison",
        "",
        "| Dataset | Baseline hard pass | Candidate hard pass | Delta | Soft warning delta | Latency delta |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for role in ("improvement", "regression", "holdout"):
        comparison = result["comparisons"][role]
        report.append(
            f"| {role} | {comparison['baseline']['hard_pass_rate']:.1%} | "
            f"{comparison['candidate']['hard_pass_rate']:.1%} | "
            f"{comparison['hard_pass_rate_delta']:+.1%} | "
            f"{comparison['soft_warning_delta']:+d} | "
            f"{comparison['mean_latency_ms_delta']:+.1f} ms |"
        )

    objectives = result["policy"].get("objectives", [])
    if objectives:
        report.extend(
            [
                "",
                "## Configured objectives",
                "",
                "| Dataset | Objective | Baseline | Candidate |",
                "|---|---|---:|---:|",
            ]
        )
        for role in ("improvement", "regression", "holdout"):
            comparison = result["comparisons"][role]
            for objective in objectives:
                name = objective["name"]
                baseline = comparison["baseline"]["objectives"][name]
                candidate_value = comparison["candidate"]["objectives"][name]
                report.append(f"| {role} | {name} | {baseline} | {candidate_value} |")

    scenario_gates = result["policy"].get("scenario_gates", [])
    if scenario_gates:
        report.extend(
            [
                "",
                "## Configured scenario gates",
                "",
                "| Dataset | Scenario | Cases | Baseline hard pass | Candidate hard pass | Delta | Soft warning delta |",
                "|---|---|---:|---:|---:|---:|---:|",
            ]
        )
        for gate in scenario_gates:
            scenario_name = gate["scenario"]
            for role in gate["roles"]:
                scenario = result["comparisons"][role]["scenarios"].get(scenario_name)
                if not scenario or not scenario.get("candidate"):
                    report.append(f"| {role} | {scenario_name} | missing | - | - | - | - |")
                    continue
                baseline = scenario["baseline"]
                candidate_value = scenario["candidate"]
                report.append(
                    f"| {role} | {scenario_name} | {candidate_value['case_count']} | "
                    f"{baseline['hard_pass_rate']:.1%} | "
                    f"{candidate_value['hard_pass_rate']:.1%} | "
                    f"{scenario['hard_pass_rate_delta']:+.1%} | "
                    f"{scenario['soft_warning_delta']:+d} |"
                )

    report.extend(["", "## Decision evidence", ""])
    report.extend(f"- {reason}" for reason in result["reasons"])
    report.extend(["", "## Run lineage", ""])
    for role in ("improvement", "regression", "holdout"):
        report.append(
            f"- `{role}`: `{result['baseline_runs'][role]['run_id']}` → "
            f"`{result['candidate_runs'][role]['run_id']}`"
        )
    (output_dir / "evolution_report.md").write_text("\n".join(report), encoding="utf-8")


def write_auto_evolution_artifacts(result: Mapping[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "auto_evolution.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = [
        f"# Automatic Evolution Report — {result['loop_id']}",
        "",
        f"- Status: **{result['status']}**",
        f"- Target: `{result['target_type']}:{result['target_id']}`",
        f"- Version: `{result['initial_version']}` → `{result['current_version']}`",
        f"- Current sandbox artifact: `{result['current_artifact']}`",
        f"- Elapsed: {result.get('usage', {}).get('elapsed_seconds', 0)} seconds",
        f"- Evolver calls: {result.get('usage', {}).get('evolver_calls', 0)}",
        f"- Retryable evolver responses: {result.get('usage', {}).get('evolver_retries', 0)}",
        "",
        "## Rounds",
        "",
    ]
    if result.get("error"):
        report[7:7] = [
            f"- Failed phase: `{result.get('failed_phase', 'unknown')}`",
            f"- Error: `{result['error']}`",
        ]
    for round_result in result["rounds"]:
        report.append(
            f"### Round {round_result['round']} — {round_result['diagnosis']['summary']}"
        )
        report.append("")
        candidates = round_result["candidates"]
        if not candidates:
            report.append("- No candidates generated")
        for candidate in candidates:
            evaluation = candidate["evaluation"]
            report.append(
                f"- `{candidate['candidate']['candidate_id']}` "
                f"→ **{evaluation['decision']}** — "
                + "; ".join(evaluation["reasons"])
            )
        report.append("")
    report.extend(
        [
            "> Accepted artifacts remain inside the sandbox. Production promotion is outside this loop.",
            "",
        ]
    )
    (output_dir / "auto_evolution_report.md").write_text(
        "\n".join(report), encoding="utf-8"
    )


def _write_jsonl(path: Path, rows: list[Mapping[str, Any]]) -> None:
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    path.write_text(content, encoding="utf-8")
