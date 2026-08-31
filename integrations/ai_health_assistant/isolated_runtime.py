from __future__ import annotations

import atexit
import hashlib
import json
import os
import shutil
import socket
import subprocess
import time
import uuid
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class IsolatedHealthRuntime:
    def __init__(self) -> None:
        self.project_root = Path(
            os.getenv("AI_HEALTH_PROJECT_ROOT", "ai-health-assistant")
        )
        self.jar = Path(
            os.getenv(
                "AI_HEALTH_JAR",
                str(self.project_root / "target" / "healthai-1.0.0.jar"),
            )
        )
        self.java = os.getenv("AI_HEALTH_JAVA", "java")
        self.runtime_root = Path(
            os.getenv(
                "AI_HEALTH_RUNTIME_ROOT",
                ".agent-eval/runtimes/ai-health-prompt-evolution",
            )
        ).resolve()
        self.timeout_seconds = float(os.getenv("AI_HEALTH_SUITE_TIMEOUT", "900"))
        self._lock = Lock()
        self._process: subprocess.Popen[str] | None = None
        self._log = None
        self._base_url = ""
        self._workdir: Path | None = None
        self._versions: dict[str, int] = {}
        self._traces: dict[str, dict[str, dict[str, Any]]] = {}
        atexit.register(self.close)

    @property
    def workdir(self) -> Path | None:
        return self._workdir

    def _request(
        self, method: str, path: str, payload: Any = None, timeout: float = 30
    ) -> Any:
        data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = Request(
            f"{self._base_url}{path}",
            data=data,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
                return json.loads(body) if body else None
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"isolated ai-health returned HTTP {error.code}: {detail}") from error

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def _copy_prompt_versions(self, target: Path) -> None:
        source = self.project_root / "prompt_versions.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        for versions in data.get("versions", {}).values():
            for version in versions:
                version.pop("versionId", None)
        target.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _prepare_workdir(self) -> Path:
        workdir = self.runtime_root / uuid.uuid4().hex[:12]
        cases_source = (
            self.project_root / ".agents" / "skills" / "health-agent-test" / "assets" / "cases"
        )
        cases_target = (
            workdir / ".agents" / "skills" / "health-agent-test" / "assets" / "cases"
        )
        cases_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(cases_source, cases_target)
        self._copy_prompt_versions(workdir / "prompt_versions.json")
        return workdir

    def _ensure_started(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if not self.jar.is_file():
            raise FileNotFoundError(f"health assistant jar not found: {self.jar}")

        self._workdir = self._prepare_workdir()
        port = self._free_port()
        self._base_url = f"http://127.0.0.1:{port}"
        self._log = (self._workdir / "server.log").open("w", encoding="utf-8")
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            [self.java, "-jar", str(self.jar), f"--server.port={port}"],
            cwd=self._workdir,
            env=os.environ.copy(),
            stdout=self._log,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=creation_flags,
        )

        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if self._process.poll() is not None:
                raise RuntimeError(f"isolated health assistant exited with {self._process.returncode}")
            try:
                self._request("GET", "/traces", timeout=2)
                return
            except Exception:
                time.sleep(1)
        raise TimeoutError("isolated health assistant did not become ready")

    @staticmethod
    def _fingerprint(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def evaluate(self, prompt: str) -> dict[str, dict[str, Any]]:
        fingerprint = self._fingerprint(prompt)
        with self._lock:
            if fingerprint in self._traces:
                return self._traces[fingerprint]
            self._ensure_started()

            version = self._versions.get(fingerprint)
            if version is None:
                registered = self._request(
                    "POST",
                    "/prompt-lab/agents/PLANNER/versions",
                    {
                        "content": prompt,
                        "changeNote": f"isolated evolution candidate {fingerprint[:8]}",
                    },
                )
                version = int(registered["version"])
                self._versions[fingerprint] = version
            self._request("PUT", f"/prompt-lab/agents/PLANNER/active/{version}")
            self._request(
                "POST",
                "/evaluate?mode=smoke",
                timeout=self.timeout_seconds,
            )
            traces = self._request("GET", "/traces")
            if not isinstance(traces, list):
                raise TypeError("isolated ai-health /traces response must be a list")
            mapped = {
                str(trace.get("caseId")): trace
                for trace in traces
                if isinstance(trace, dict) and trace.get("caseId")
            }
            self._traces[fingerprint] = mapped
            return mapped

    def close(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self._log is not None:
            self._log.close()
            self._log = None
