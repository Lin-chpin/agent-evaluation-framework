from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_eval.openai_trace import load_openai_traces, normalize_openai_trace


class OpenAITraceTest(unittest.TestCase):
    def test_normalizes_agent_llm_and_tool_spans(self) -> None:
        trace = normalize_openai_trace(
            [
                {
                    "trace_id": "trace-demo",
                    "id": "span-agent",
                    "started_at": "2026-09-14T10:00:00+00:00",
                    "ended_at": "2026-09-14T10:00:00.120000+00:00",
                    "span_data": {"type": "agent", "name": "ResearchAgent", "output": "final answer"},
                },
                {
                    "trace_id": "trace-demo",
                    "id": "span-generation",
                    "started_at": "2026-09-14T10:00:00.010000+00:00",
                    "ended_at": "2026-09-14T10:00:00.100000+00:00",
                    "span_data": {
                        "type": "generation",
                        "model": "gpt-test",
                        "usage": {"input_tokens": 12, "output_tokens": 8},
                    },
                },
                {
                    "trace_id": "trace-demo",
                    "id": "span-tool",
                    "span_data": {
                        "type": "function",
                        "name": "search_docs",
                        "input": {"query": "eval"},
                        "output": {"hits": 2},
                    },
                },
            ],
            target_id="demo-agent",
            target_version="v1",
        )

        self.assertEqual(trace.trace_id, "trace-demo")
        self.assertEqual(trace.final_output, "final answer")
        self.assertEqual(
            [event.module for event in trace.events],
            ["OpenAIAgent", "OpenAILLM", "OpenAITool"],
        )
        self.assertEqual(trace.fields["span_count"], 3)
        self.assertEqual(trace.fields["llm_call_count"], 1)
        self.assertEqual(trace.fields["tool_call_count"], 1)
        self.assertEqual(trace.fields["total_tokens"], 20)
        self.assertAlmostEqual(trace.fields["duration_ms"], 120.0, places=2)
        self.assertEqual(trace.target_id, "demo-agent")

    def test_loads_jsonl_and_groups_traces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "traces.jsonl"
            path.write_text(
                "".join(
                    json.dumps(
                        {"trace_id": trace_id, "span_data": {"type": "agent", "output": output}}
                    )
                    + "\n"
                    for trace_id, output in (("trace-a", "a"), ("trace-b", "b"), ("trace-a", "a2"))
                ),
                encoding="utf-8",
            )

            traces = load_openai_traces(path)

        self.assertEqual(set(traces), {"trace-a", "trace-b"})
        self.assertEqual(traces["trace-a"].final_output, "a2")
        self.assertEqual(traces["trace-b"].final_output, "b")


if __name__ == "__main__":
    unittest.main()
