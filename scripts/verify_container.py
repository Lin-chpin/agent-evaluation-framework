from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_eval import run_agent_container


PROBE = """
import json
import socket
from pathlib import Path

checks = {"workspace_read": Path("/workspace/probe.txt").read_text() == "probe"}
try:
    Path("/workspace/probe.txt").write_text("changed")
    checks["workspace_read_only"] = False
except OSError:
    checks["workspace_read_only"] = True
try:
    Path("/blocked.txt").write_text("blocked")
    checks["root_read_only"] = False
except OSError:
    checks["root_read_only"] = True
Path("/tmp/allowed.txt").write_text("allowed")
checks["tmp_writable"] = Path("/tmp/allowed.txt").read_text() == "allowed"
connection = socket.socket()
connection.settimeout(0.5)
try:
    connection.connect(("1.1.1.1", 53))
    checks["network_disabled"] = False
except OSError:
    checks["network_disabled"] = True
finally:
    connection.close()
print(json.dumps(checks, sort_keys=True))
raise SystemExit(0 if all(checks.values()) else 2)
"""


def git_value(*arguments: str) -> str | None:
    completed = subprocess.run(
        ["git", *arguments], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the real container isolation smoke")
    parser.add_argument("--engine", default="docker")
    parser.add_argument("--image", default="python:3.12-alpine")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    source_revision = git_value("rev-parse", "HEAD")
    source_status = git_value("status", "--porcelain")
    checks = {}
    completed: subprocess.CompletedProcess[str] | None = None
    error: str | None = None
    try:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            probe = workspace / "probe.txt"
            probe.write_text("probe", encoding="utf-8")
            workspace.chmod(0o755)
            probe.chmod(0o644)
            completed = run_agent_container(
                args.image,
                ["python", "-c", PROBE],
                workspace,
                engine=args.engine,
                timeout_seconds=30,
            )
        if completed.stdout.strip():
            checks = json.loads(completed.stdout.strip().splitlines()[-1])
    except Exception as exception:
        error = f"{type(exception).__name__}: {exception}"
    image = subprocess.run(
        [args.engine, "image", "inspect", args.image, "--format", "{{json .RepoDigests}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    evidence = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "engine": args.engine,
        "image": args.image,
        "image_repo_digests": image.stdout.strip() if image.returncode == 0 else None,
        "source_revision": source_revision,
        "source_is_dirty": bool(source_status) if source_status is not None else None,
        "container_returncode": completed.returncode if completed else None,
        "stdout": completed.stdout if completed else "",
        "stderr": completed.stderr if completed else "",
        "error": error,
        "checks": checks,
        "status": "passed"
        if completed and completed.returncode == 0 and checks and all(checks.values())
        else "failed",
    }
    content = json.dumps(evidence, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    print(content)
    if completed and completed.stderr:
        print(completed.stderr, file=sys.stderr)
    if evidence["status"] != "passed" and os.getenv("GITHUB_ACTIONS"):
        diagnostic = json.dumps(
            {
                "returncode": evidence["container_returncode"],
                "error": evidence["error"],
                "stderr": evidence["stderr"],
                "checks": evidence["checks"],
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error file=scripts/verify_container.py,title=Container smoke failed::{diagnostic}")
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
