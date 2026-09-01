from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .model import TextCandidate, TextFileOperation, to_jsonable
from .locking import exclusive_file_lock
from .text_file_operations import (
    apply_text_file_operations,
    validate_text_file_operations,
)


def safe_name(value: str, field: str) -> str:
    value = value.strip()
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError(f"{field} must be a safe non-empty name")
    return value


def read_artifact(path: Path) -> str | dict[str, str]:
    if path.is_symlink():
        raise ValueError(f"symbolic links are not supported: {path}")
    if path.is_file():
        return path.read_text(encoding="utf-8")
    if not path.is_dir():
        raise FileNotFoundError(f"artifact does not exist: {path}")
    files: dict[str, str] = {}
    for item in sorted(path.rglob("*")):
        if item.is_symlink():
            raise ValueError(f"symbolic links are not supported: {item}")
        if item.is_file():
            files[item.relative_to(path).as_posix()] = item.read_text(encoding="utf-8")
    return files


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
        read_artifact(source)
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
        if candidate.operations:
            if candidate.files:
                raise ValueError("candidate must use files or operations, not both")
            if current_artifact is None or not current_artifact.is_dir():
                raise ValueError("file operations require a directory artifact")
            current = read_artifact(current_artifact)
            if not isinstance(current, dict):
                raise ValueError("file operations require a directory artifact")
            validated = validate_text_file_operations(current, candidate.operations)
            manifest_path = target.parent / "artifact-manifest.json"
            expected_operations = to_jsonable(candidate.operations)
            if target.exists():
                if not manifest_path.is_file():
                    raise ValueError(f"candidate directory exists without manifest: {target}")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if (
                    manifest.get("operations") != expected_operations
                    or manifest.get("tree_sha256") != _tree_sha256(target)
                ):
                    raise ValueError(f"candidate artifact already exists with different content: {target}")
                return target
            target.parent.mkdir(parents=True)
            shutil.copytree(current_artifact, target)
            apply_text_file_operations(target, validated)
            manifest_path.write_text(
                json.dumps(
                    {"operations": expected_operations, "tree_sha256": _tree_sha256(target)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return target
        if candidate.files:
            if current_artifact is None or not current_artifact.is_dir():
                raise ValueError("multi-file candidate requires a directory artifact")
            manifest_path = target.parent / "artifact-manifest.json"
            expected_files = dict(candidate.files)
            current = read_artifact(current_artifact)
            if not isinstance(current, dict):
                raise ValueError("multi-file candidate requires a directory artifact")
            validated = validate_text_file_operations(
                current,
                tuple(
                    TextFileOperation("write", relative, content)
                    for relative, content in expected_files.items()
                ),
            )
            if target.exists():
                if not manifest_path.is_file():
                    raise ValueError(f"candidate directory exists without manifest: {target}")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("files") != expected_files or manifest.get("tree_sha256") != _tree_sha256(target):
                    raise ValueError(f"candidate artifact already exists with different content: {target}")
                return target
            target.parent.mkdir(parents=True)
            shutil.copytree(current_artifact, target)
            apply_text_file_operations(target, validated)
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
