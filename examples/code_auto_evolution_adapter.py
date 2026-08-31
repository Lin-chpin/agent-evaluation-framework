from __future__ import annotations

import json
import sys
from pathlib import Path

from agent_eval import (
    AutoEvolutionAdapter,
    EvolutionDiagnosis,
    NormalizedTrace,
    ProjectAdapter,
    Rule,
    TextCandidate,
    TraceEvent,
    run_agent_process,
)


ROOT = Path(__file__).parent


def _code(keywords: str) -> str:
    return f'''from __future__ import annotations

import json
import sys


KEYWORDS = {{{keywords}}}
message = sys.argv[1].lower()
route = "SUPPORT" if any(keyword in message for keyword in KEYWORDS) else "GENERAL"
print(json.dumps({{"route": route}}))
'''


def build_adapter(artifact: Path, version: str) -> ProjectAdapter:
    def call_agent(case, context):
        completed = run_agent_process(
            [sys.executable, str(artifact.resolve()), case.payload["message"]],
            artifact.parent,
            timeout_seconds=context.timeout_seconds,
        )
        if completed.returncode:
            raise RuntimeError(completed.stderr or f"agent exited with {completed.returncode}")
        return json.loads(completed.stdout)

    def read_trace(handle, case):
        route = handle["route"]
        return NormalizedTrace(
            f"process-{case.case_id}-{version}",
            {"route": route},
            (TraceEvent("CodeAgentProcess", "route"),),
            {"route": route, "quality": 0.9 if route == case.expected["route"] else 0.2},
            target_type="agent",
            target_id="process-router",
            target_version=version,
        )

    return ProjectAdapter(
        f"process-router-{version}",
        call_agent,
        read_trace,
        (Rule("route", "fields.route", expected="route"),),
        (),
    )


def diagnose(_):
    return EvolutionDiagnosis(
        "The code agent misses password-reset routing.",
        "agent",
        "process-router",
        ("IMPROVE",),
    )


def generate_candidates(_, __, ___):
    return (
        TextCandidate(
            "replace-code",
            "2-bad",
            _code('"password"'),
            "replace the existing behavior",
            change_type="code",
        ),
        TextCandidate(
            "extend-code",
            "2",
            _code('"billing", "password"'),
            "preserve existing behavior and add password routing",
            change_type="code",
        ),
    )


AUTO_EVOLUTION = AutoEvolutionAdapter(
    "process-code-agent",
    ROOT / "code_agent.py",
    "1",
    "agent",
    "process-router",
    diagnose,
    generate_candidates,
    build_adapter,
)
