from __future__ import annotations

import json
import subprocess
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .model import TestImpactAssessment


@dataclass(frozen=True)
class DiffSnapshot:
    raw_diff: str
    files: tuple[str, ...]
    additions: int
    deletions: int
    categories: dict[str, int]
    extensions: dict[str, int]
    signals: tuple[str, ...]

    def sanitized_summary(self) -> str:
        return json.dumps(
            {
                "changed_file_count": len(self.files),
                "additions": self.additions,
                "deletions": self.deletions,
                "file_categories": self.categories,
                "file_extensions": self.extensions,
                "impact_signals": self.signals,
                "privacy": "No source text, values, URLs, or file paths are included.",
            },
            ensure_ascii=False,
            sort_keys=True,
        )


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "git command failed")
    return result.stdout


def _category(path: str) -> str:
    value = path.lower()
    if value.startswith(("docs/", "readme", "changelog", "license")):
        return "documentation"
    if any(part in value for part in ("ui/", "frontend/", "web/", "templates/", "styles/")):
        return "ui"
    if any(part in value for part in ("report", "render", "markdown", "html")):
        return "reporting"
    if any(part in value for part in ("test", "case", "fixture", "gold")):
        return "tests"
    if any(part in value for part in ("prompt", "planner", "safety", "security", "guardrail")):
        return "control_logic"
    if any(part in value for part in ("rag", "retriev", "generat", "schema", "model", "agent")):
        return "agent_core"
    if value.endswith((".yml", ".yaml", ".toml", ".json")):
        return "configuration"
    return "source"


def read_git_diff(repository: Path | str = ".", base: str = "HEAD") -> DiffSnapshot:
    repository = Path(repository).resolve()
    raw_diff = _git(repository, "diff", "--no-ext-diff", "--unified=1", base)
    numstat = _git(repository, "diff", "--numstat", base)
    untracked = tuple(
        line for line in _git(repository, "ls-files", "--others", "--exclude-standard").splitlines() if line
    )

    files: list[str] = []
    additions = deletions = 0
    for line in numstat.splitlines():
        added, removed, path = line.split("\t", 2)
        files.append(path)
        additions += int(added) if added.isdigit() else 0
        deletions += int(removed) if removed.isdigit() else 0

    untracked_diff: list[str] = []
    for relative in untracked:
        files.append(relative)
        path = repository / relative
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            content = ""
        if content and len(content.encode("utf-8")) <= 256_000:
            lines = content.splitlines()
            additions += len(lines)
            untracked_diff.append(
                f"diff --git a/{relative} b/{relative}\nnew file mode 100644\n"
                f"--- /dev/null\n+++ b/{relative}\n"
                + "\n".join(f"+{line}" for line in lines)
            )

    unique_files = tuple(dict.fromkeys(files))
    categories = dict(Counter(_category(path) for path in unique_files))
    extensions = dict(
        Counter((PurePosixPath(path).suffix.lower() or "[none]") for path in unique_files)
    )
    signals = tuple(sorted(categories))
    combined = raw_diff
    if untracked_diff:
        combined += ("\n" if combined else "") + "\n".join(untracked_diff)
    return DiffSnapshot(
        raw_diff=combined,
        files=unique_files,
        additions=additions,
        deletions=deletions,
        categories=categories,
        extensions=extensions,
        signals=signals,
    )


def assess_rules(snapshot: DiffSnapshot) -> TestImpactAssessment:
    categories = set(snapshot.categories)
    low_only = categories <= {"documentation", "ui", "reporting", "tests"}
    high_risk = "control_logic" in categories
    full_impact = "agent_core" in categories

    if full_impact:
        mode = "full"
        reason = "Agent generation, retrieval, model, or result-structure code changed."
    elif high_risk:
        mode = "regression"
        reason = "Planner, prompt, safety, or guardrail behavior changed."
    elif low_only:
        mode = "smoke"
        reason = "Only documentation, UI, reporting, or test assets changed."
    else:
        mode = "regression"
        reason = "General source or configuration changes need protected-case coverage."

    return TestImpactAssessment(
        mode=mode,
        confidence=1.0,
        risk="high" if high_risk else ("medium" if mode != "smoke" else "low"),
        reasons=(reason,),
        source="rules",
        input_kind="metadata",
    )
