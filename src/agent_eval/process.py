from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Mapping, Sequence


DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
TRUNCATED_OUTPUT_MARKER = "\n...[output truncated]"


def _read_output(stream, max_output_bytes: int, encoding: str) -> str:
    stream.seek(0, os.SEEK_END)
    was_truncated = stream.tell() > max_output_bytes
    stream.seek(0)
    output = stream.read(max_output_bytes).decode(encoding, errors="replace")
    return output + TRUNCATED_OUTPUT_MARKER if was_truncated else output


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    # Agent commands may spawn servers or tools; killing only the parent would leak them into later evaluations.
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if process.poll() is None:
            process.kill()
    else:
        os.killpg(process.pid, signal.SIGKILL)


def run_agent_process(
    command: Sequence[str],
    cwd: Path,
    *,
    input_text: str | None = None,
    timeout_seconds: float = 30,
    environment: Mapping[str, str] | None = None,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    if not command:
        raise ValueError("agent command must not be empty")
    if not cwd.is_dir():
        raise FileNotFoundError(f"agent working directory does not exist: {cwd}")
    if timeout_seconds <= 0:
        raise ValueError("agent timeout must be positive")
    if max_output_bytes <= 0:
        raise ValueError("agent output limit must be positive")

    creation_flags = 0
    start_new_session = os.name != "nt"
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        creation_flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # Temporary files keep an output-heavy child from growing the parent process without bound.
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        # Keep shell parsing out of the trust boundary; adapters must provide an explicit argument list.
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env={**os.environ, **(environment or {})},
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
            start_new_session=start_new_session,
            creationflags=creation_flags,
        )
        try:
            process.communicate(input_text, timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            _terminate_process_tree(process)
            process.communicate()
            stdout = _read_output(stdout_file, max_output_bytes, process.encoding)
            stderr = _read_output(stderr_file, max_output_bytes, process.encoding)
            raise subprocess.TimeoutExpired(
                command,
                timeout_seconds,
                output=stdout,
                stderr=stderr,
            ) from error
        stdout = _read_output(stdout_file, max_output_bytes, process.encoding)
        stderr = _read_output(stderr_file, max_output_bytes, process.encoding)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
