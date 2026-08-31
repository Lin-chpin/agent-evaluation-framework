from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .model import TextCandidate
from .locking import exclusive_file_lock


def safe_name(value: str, field: str) -> str:
    value = value.strip()
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError(f"{field} must be a safe non-empty name")
    return value


def _safe_relative_file(value: str) -> Path:
    path = Path(value)
    if not value.strip() or path.is_absolute() or ".." in path.parts or path.name in {"", ".", ".."}:
        raise ValueError(f"candidate file must be a safe relative path: {value}")
    return path


def read_artifact(path: Path) -> str | dict[str, str]:
    if path.is_file():
        return path.read_text(encoding="utf-8")
    if not path.is_dir():
        raise FileNotFoundError(f"artifact does not exist: {path}")
    return {
        file.relative_to(path).as_posix(): file.read_text(encoding="utf-8")
        for file in sorted(item for item in path.rglob("*") if item.is_file())
    }


def _tree_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    for relative, content in read_artifact(path).items():
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content.encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


class TextArtifactWorkspace:
    def __init__(self, root: Path):
        self.root = root

    @contextmanager
    def lock_loop(self, loop_id: str) -> Iterator[Path]:
        lock_path = self.root / ".locks" / f"{safe_name(loop_id, 'loop_id')}.lock"
        with exclusive_file_lock(lock_path, f"auto evolution loop {loop_id}"):
            yield lock_path

    def snapshot(self, loop_id: str, source: Path) -> Path:
        loop_root = self.root / safe_name(loop_id, "loop_id")
        if loop_root.exists():
            raise ValueError(f"auto evolution workspace already exists: {loop_root}")
        target = loop_root / "baseline" / source.name
        target.parent.mkdir(parents=True)
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
        return target

    def stage(
        self,
        loop_id: str,
        round_number: int,
        candidate: TextCandidate,
        name: str,
        current_artifact: Path | None = None,
    ) -> Path:
        candidate_id = safe_name(candidate.candidate_id, "candidate_id")
        target = self.root / loop_id / "candidates" / f"round-{round_number}" / candidate_id / name
        if candidate.files:
            if current_artifact is None or not current_artifact.is_dir():
                raise ValueError("multi-file candidate requires a directory artifact")
            manifest_path = target.parent / "artifact-manifest.json"
            expected_files = dict(candidate.files)
            if target.exists():
                if not manifest_path.is_file():
                    raise ValueError(f"candidate directory exists without manifest: {target}")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("files") != expected_files or manifest.get("tree_sha256") != _tree_sha256(target):
                    raise ValueError(f"candidate artifact already exists with different content: {target}")
                return target
            target.parent.mkdir(parents=True)
            shutil.copytree(current_artifact, target)
            for relative, content in expected_files.items():
                destination = target / _safe_relative_file(relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(content, encoding="utf-8")
            manifest_path.write_text(
                json.dumps(
                    {"files": expected_files, "tree_sha256": _tree_sha256(target)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return target
        if target.exists():
            if target.read_text(encoding="utf-8") == candidate.content:
                return target
            raise ValueError(f"candidate artifact already exists with different content: {target}")
        target.parent.mkdir(parents=True)
        target.write_text(candidate.content, encoding="utf-8")
        return target

    def save_checkpoint(self, loop_id: str, state: Mapping[str, Any]) -> Path:
        path = self.root / safe_name(loop_id, "loop_id") / "checkpoint.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return path

    def load_checkpoint(self, loop_id: str) -> dict[str, Any]:
        path = self.root / safe_name(loop_id, "loop_id") / "checkpoint.json"
        if not path.is_file():
            raise FileNotFoundError(f"auto evolution checkpoint does not exist: {path}")
        return json.loads(path.read_text(encoding="utf-8"))
