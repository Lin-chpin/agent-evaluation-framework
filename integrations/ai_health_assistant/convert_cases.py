from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping


def convert_case(value: Mapping[str, Any]) -> dict[str, Any]:
    checks = value.get("checks") or {}
    route = str(value["expected_route"])
    return {
        "id": value["id"],
        "scenario": value.get("category", "default"),
        "input": {"message": value["input"]},
        "expected": {
            "route": route,
            "path": value["expected_path"],
            "need_follow_up": bool(value.get("expected_follow_up")),
            "safety_blocked": route.upper() == "RISK",
            "must_include": checks.get("must_include", []),
            "must_not_include": checks.get("must_not_include", []),
        },
        "metadata": {
            "user_profile": value.get("user_profile", "unknown"),
            "expected_safety_level": value.get("expected_safety_level"),
            "diagnosis_hints": value.get("diagnosis_hints", []),
            "provenance": "ai-health-assistant project case",
        },
    }


def convert_file(source: Path, output: Path) -> int:
    rows = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(convert_case(json.loads(line)))
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError(f"invalid health case at {source}:{line_number}: {error}") from error
    if not rows:
        raise ValueError(f"case file is empty: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    count = convert_file(args.source, args.output)
    print(f"converted {count} cases to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
