from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .process import run_agent_process


def build_container_command(
    image: str,
    command: Sequence[str],
    workspace: Path,
    *,
    engine: str = "docker",
    memory: str = "512m",
    cpus: float = 1,
    pids_limit: int = 128,
    network_enabled: bool = False,
    workspace_writable: bool = False,
) -> list[str]:
    if not image.strip() or not command:
        raise ValueError("container image and command are required")
    if not workspace.is_dir():
        raise FileNotFoundError(f"container workspace does not exist: {workspace}")
    if cpus <= 0 or pids_limit < 1:
        raise ValueError("container CPU and PID limits must be positive")

    mount = f"type=bind,src={workspace.resolve()},dst=/workspace"
    if not workspace_writable:
        mount += ",readonly"
    return [
        engine,
        "run",
        "--rm",
        "--network",
        "bridge" if network_enabled else "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--pids-limit",
        str(pids_limit),
        "--memory",
        memory,
        "--cpus",
        str(cpus),
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=64m",
        "--mount",
        mount,
        "--workdir",
        "/workspace",
        image,
        *command,
    ]


def run_agent_container(
    image: str,
    command: Sequence[str],
    workspace: Path,
    *,
    engine: str = "docker",
    timeout_seconds: float = 30,
    memory: str = "512m",
    cpus: float = 1,
    pids_limit: int = 128,
    network_enabled: bool = False,
    workspace_writable: bool = False,
):
    return run_agent_process(
        build_container_command(
            image,
            command,
            workspace,
            engine=engine,
            memory=memory,
            cpus=cpus,
            pids_limit=pids_limit,
            network_enabled=network_enabled,
            workspace_writable=workspace_writable,
        ),
        workspace,
        timeout_seconds=timeout_seconds,
    )
