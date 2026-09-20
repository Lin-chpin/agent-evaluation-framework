from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval.openai_trace import load_openai_traces
from agent_eval.openai_trace_processor import OpenAITraceProcessor


class _FakeSpan:
    def __init__(self, payload: dict):
        self.payload = payload

    def export(self) -> dict:
        return self.payload


class OpenAITraceProcessorTest(unittest.TestCase):
    def test_writes_sdk_spans_that_the_loader_can_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "openai-traces.jsonl"
            processor = OpenAITraceProcessor(path)
            processor.on_span_end(
                _FakeSpan(
                    {
                        "trace_id": "trace-live",
                        "id": "span-agent",
                        "span_data": {"type": "agent", "output": "done"},
                    }
                )
            )
            processor.force_flush()
            processor.shutdown()

            traces = load_openai_traces(path)

        self.assertEqual(traces["trace-live"].final_output, "done")
        self.assertEqual(traces["trace-live"].fields["agent_span_count"], 1)


if __name__ == "__main__":
    unittest.main()
