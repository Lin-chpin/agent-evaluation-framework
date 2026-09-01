from __future__ import annotations

from pathlib import Path
from typing import Mapping, Sequence

from .model import TextFileOperation


ValidatedTextFileOperation = tuple[TextFileOperation, Path, Path | None]


def safe_relative_file(value: str) -> Path:
    path = Path(value)
    if (
        not value.strip()
        or "\\" in value
        or path.is_absolute()
        or ".." in path.parts
        or path.name in {"", ".", ".."}
    ):
        raise ValueError(f"candidate file must be a safe relative path: {value}")
    return path


def validate_text_file_operations(
    current_files: Mapping[str, str], operations: Sequence[TextFileOperation]
) -> list[ValidatedTextFileOperation]:
    files = set(current_files)
    touched: set[str] = set()
    validated: list[ValidatedTextFileOperation] = []
    for operation in operations:
        path = safe_relative_file(operation.path)
        relative = path.as_posix()
        destination = (
            safe_relative_file(operation.destination)
            if operation.destination is not None
            else None
        )
        affected = {relative}
        if destination is not None:
            affected.add(destination.as_posix())
        if touched.intersection(affected):
            raise ValueError(f"candidate file operations conflict at: {sorted(affected)}")
        touched.update(affected)
        if operation.operation == "write":
            files.add(relative)
        elif operation.operation == "delete":
            if relative not in files:
                raise ValueError(f"delete source does not exist: {relative}")
            files.remove(relative)
        else:
            destination_relative = destination.as_posix()
            if relative not in files:
                raise ValueError(f"move source does not exist: {relative}")
            if destination_relative in files:
                raise ValueError(f"move destination already exists: {destination_relative}")
            files.remove(relative)
            files.add(destination_relative)
        validated.append((operation, path, destination))
    for relative in files:
        parents = (parent.as_posix() for parent in Path(relative).parents if parent != Path("."))
        if any(parent in files for parent in parents):
            raise ValueError(f"candidate file conflicts with a directory path: {relative}")
    return validated


def apply_text_file_operations(
    root: Path, operations: Sequence[ValidatedTextFileOperation]
) -> None:
    for operation, relative, destination in operations:
        source = root / relative
        if operation.operation == "write":
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_text(operation.content, encoding="utf-8")
        elif operation.operation == "delete":
            source.unlink()
        else:
            moved = root / destination
            moved.parent.mkdir(parents=True, exist_ok=True)
            source.replace(moved)
