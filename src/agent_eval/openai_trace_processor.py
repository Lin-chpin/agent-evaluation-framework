from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping


class OpenAITraceProcessor:
    """Write OpenAI Agents SDK span exports as JSONL without importing the SDK."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._handle = self.path.open("a", encoding="utf-8")

    def on_trace_start(self, trace: Any) -> None:
        return None

    def on_trace_end(self, trace: Any) -> None:
        return None

    def on_span_start(self, span: Any) -> None:
        return None

    def on_span_end(self, span: Any) -> None:
        exported = span.export() or {}
        if not isinstance(exported, Mapping):
            raise TypeError("OpenAI span export must be an object")
        encoded = json.dumps(dict(exported), ensure_ascii=False, default=str)
        with self._lock:
            self._handle.write(encoded + "\n")

    def force_flush(self) -> None:
        with self._lock:
            self._handle.flush()

    def shutdown(self) -> None:
        with self._lock:
            if not self._handle.closed:
                self._handle.flush()
                self._handle.close()


def install_openai_trace_processor(path: str | Path) -> OpenAITraceProcessor:
    """Install the local processor into OpenAI Agents SDK when it is available."""
    try:
        from agents import set_trace_processors
    except ImportError as error:
        raise RuntimeError(
            "installing an OpenAI trace processor requires the optional openai-agents package"
        ) from error
    processor = OpenAITraceProcessor(path)
    set_trace_processors([processor])
    return processor
