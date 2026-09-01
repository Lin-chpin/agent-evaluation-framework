from __future__ import annotations

import locale
import os
import signal
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Mapping, Sequence


DEFAULT_MAX_OUTPUT_BYTES = 1024 * 1024
TRUNCATED_OUTPUT_MARKER = "\n...[output truncated]"


class OutputLimitExceeded(RuntimeError):
    def __init__(self, command: Sequence[str], stream: str, stdout: str, stderr: str):
        super().__init__(f"agent {stream} exceeded the configured output limit")
        self.command = tuple(command)
        self.stream = stream
        self.stdout = stdout
        self.stderr = stderr


def _decode_output(output: bytearray, truncated: bool, encoding: str) -> str:
    value = bytes(output).decode(encoding, errors="replace")
    return value + TRUNCATED_OUTPUT_MARKER if truncated else value


def _terminate_process_tree(process: subprocess.Popen) -> None:
    # Agent commands may spawn servers or tools; killing only the parent would leak them into later evaluations.
    if process.poll() is not None:
        return
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
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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

    encoding = locale.getpreferredencoding(False)
    with tempfile.TemporaryFile() as stdin_file:
        if input_text is not None:
            stdin_file.write(input_text.encode(encoding))
            stdin_file.seek(0)
        # Keep shell parsing out of the trust boundary; adapters must provide an explicit argument list.
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            env={**os.environ, **(environment or {})},
            stdin=stdin_file,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=start_new_session,
            creationflags=creation_flags,
        )
        outputs = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = {"stdout": False, "stderr": False}
        state: dict[str, str | None] = {"reason": None, "stream": None}
        state_lock = threading.Lock()

        def stop(reason: str, stream_name: str | None = None) -> None:
            with state_lock:
                if state["reason"] is not None:
                    return
                state.update(reason=reason, stream=stream_name)
            _terminate_process_tree(process)

        def capture(stream_name: str) -> None:
            stream = getattr(process, stream_name)
            assert stream is not None
            try:
                while chunk := stream.read1(65536):
                    remaining = max_output_bytes - len(outputs[stream_name])
                    outputs[stream_name].extend(chunk[:remaining])
                    if len(chunk) > remaining:
                        truncated[stream_name] = True
                        stop("output_limit", stream_name)
                        return
            finally:
                stream.close()

        readers = [
            threading.Thread(target=capture, args=(name,), daemon=True)
            for name in ("stdout", "stderr")
        ]
        for reader in readers:
            reader.start()
        try:
            process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            stop("timeout")
            process.wait()
            for reader in readers:
                reader.join()
            stdout = _decode_output(outputs["stdout"], truncated["stdout"], encoding)
            stderr = _decode_output(outputs["stderr"], truncated["stderr"], encoding)
            if state["reason"] == "output_limit":
                raise OutputLimitExceeded(command, str(state["stream"]), stdout, stderr) from error
            raise subprocess.TimeoutExpired(
                command,
                timeout_seconds,
                output=stdout,
                stderr=stderr,
            ) from error
        for reader in readers:
            reader.join()
        stdout = _decode_output(outputs["stdout"], truncated["stdout"], encoding)
        stderr = _decode_output(outputs["stderr"], truncated["stderr"], encoding)
        if state["reason"] == "output_limit":
            raise OutputLimitExceeded(command, str(state["stream"]), stdout, stderr)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
