from __future__ import annotations

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
        shutil.copy2(source, target)
        return target

    def stage(self, loop_id: str, round_number: int, candidate: TextCandidate, name: str) -> Path:
        candidate_id = safe_name(candidate.candidate_id, "candidate_id")
        target = self.root / loop_id / "candidates" / f"round-{round_number}" / candidate_id / name
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
