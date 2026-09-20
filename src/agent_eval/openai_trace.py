from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import NormalizedTrace, TraceEvent


_SPAN_MODULES = {
    "agent": "OpenAIAgent",
    "generation": "OpenAILLM",
    "response": "OpenAILLM",
    "function": "OpenAITool",
    "mcp_tools": "OpenAITool",
    "handoff": "OpenAIHandoff",
    "guardrail": "OpenAIGuardrail",
}


def normalize_openai_trace(
    spans: Iterable[Mapping[str, Any]],
    *,
    target_id: str = "openai-agent",
    target_version: str = "unknown",
) -> NormalizedTrace:
    """Convert exported OpenAI Agents SDK spans into one project Trace."""
    items = [dict(span) for span in spans]
    if not items:
        raise ValueError("OpenAI trace requires at least one span")

    trace_ids = {_trace_id(item) for item in items}
    trace_ids.discard("")
    if len(trace_ids) != 1:
        raise ValueError("OpenAI trace spans must share exactly one trace_id")
    trace_id = trace_ids.pop()

    events: list[TraceEvent] = []
    span_types: list[str] = []
    outputs: list[tuple[int, int, Any]] = []
    starts: list[float] = []
    ends: list[float] = []
    input_tokens = output_tokens = 0
    token_spans = [item for item in items if _span_type(item) == "generation"]
    if not token_spans:
        token_spans = [item for item in items if _span_type(item) == "response"]

    for index, item in enumerate(items):
        span_type = _span_type(item)
        span_types.append(span_type)
        attributes = _attributes(item)
        duration_ms, started, ended = _duration(item)
        if started is not None:
            starts.append(started)
        if ended is not None:
            ends.append(ended)

        error = _error_message(item)
        events.append(
            TraceEvent(
                module=_SPAN_MODULES.get(span_type, "OpenAITrace"),
                action=_span_name(item, span_type),
                status="error" if error else "ok",
                duration_ms=duration_ms,
                error=error,
                fields={
                    **attributes,
                    "span_type": span_type,
                    "span_id": item.get("id", item.get("span_id", "")),
                    "parent_span_id": item.get("parent_id", item.get("parent_span_id", "")),
                },
            )
        )

        output = _span_output(item, attributes)
        if output is not None:
            priority = {"agent": 3, "generation": 2, "response": 2}.get(span_type, 1)
            outputs.append((priority, index, output))

        if item.get("error") or _status_is_error(item.get("status")):
            continue
        if item in token_spans:
            usage = _usage(item, attributes)
            input_tokens += usage[0]
            output_tokens += usage[1]

    error_count = sum(event.status == "error" for event in events)
    span_counts = {name: span_types.count(name) for name in set(span_types)}
    fields: dict[str, Any] = {
        "span_count": len(items),
        "agent_span_count": span_counts.get("agent", 0),
        "llm_call_count": span_counts.get("generation", 0) + span_counts.get("response", 0),
        "tool_call_count": span_counts.get("function", 0) + span_counts.get("mcp_tools", 0),
        "handoff_count": span_counts.get("handoff", 0),
        "error_count": error_count,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }
    if starts and ends:
        fields["duration_ms"] = round((max(ends) - min(starts)) * 1000, 4)

    first_attributes = _attributes(items[0])
    for source, name in (("agent.workflow.name", "workflow_name"), ("agent.workflow.group_id", "group_id")):
        if source in first_attributes:
            fields[name] = first_attributes[source]

    return NormalizedTrace(
        trace_id=trace_id,
        final_output=max(outputs, default=(0, 0, None))[2],
        events=tuple(events),
        fields=fields,
        feedback=_feedback(items),
        target_type="agent",
        target_id=target_id,
        target_version=target_version,
        raw={"source": "openai-agents-sdk", "spans": items},
    )


def load_openai_traces(
    path: str | Path,
    *,
    target_id: str = "openai-agent",
    target_version: str = "unknown",
) -> dict[str, NormalizedTrace]:
    """Load exported OpenAI Agents SDK spans from JSONL, grouped by trace."""
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    source = Path(path)
    with source.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"invalid OpenAI trace at {source}:{line_number}: {error}") from error
            if not isinstance(item, Mapping):
                raise ValueError(f"OpenAI trace at {source}:{line_number} must be an object")
            trace_id = _trace_id(item)
            if not trace_id:
                raise ValueError(f"OpenAI trace at {source}:{line_number} requires trace_id")
            grouped[trace_id].append(item)

    return {
        trace_id: normalize_openai_trace(spans, target_id=target_id, target_version=target_version)
        for trace_id, spans in grouped.items()
    }


def _trace_id(item: Mapping[str, Any]) -> str:
    return str(item.get("trace_id") or item.get("traceId") or "").strip()


def _span_data(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("span_data")
    return value if isinstance(value, Mapping) else {}


def _attributes(item: Mapping[str, Any]) -> dict[str, Any]:
    value = item.get("attributes")
    return dict(value) if isinstance(value, Mapping) else {}


def _span_type(item: Mapping[str, Any]) -> str:
    data = _span_data(item)
    if data.get("type"):
        return str(data["type"]).lower()
    value = _attributes(item).get("inference.observation_kind") or _attributes(item).get(
        "openinference.span.kind"
    )
    return {
        "AGENT": "agent",
        "LLM": "generation",
        "TOOL": "function",
        "GUARDRAIL": "guardrail",
    }.get(str(value).upper(), "custom")


def _span_name(item: Mapping[str, Any], span_type: str) -> str:
    data = _span_data(item)
    attributes = _attributes(item)
    return str(
        data.get("name")
        or item.get("name")
        or attributes.get("sdk.span.name")
        or attributes.get("tool.name")
        or span_type
    )


def _span_output(item: Mapping[str, Any], attributes: Mapping[str, Any]) -> Any:
    data = _span_data(item)
    for value in (
        data.get("output"),
        data.get("output_messages"),
        attributes.get("output.value"),
        attributes.get("llm.output_messages"),
        attributes.get("llm.output"),
    ):
        if value is not None:
            return value
    return None


def _error_message(item: Mapping[str, Any]) -> str | None:
    error = item.get("error")
    if isinstance(error, Mapping):
        return str(error.get("message") or "OpenAI span failed") if error else None
    if error:
        return str(error)
    status = item.get("status")
    if isinstance(status, Mapping) and _status_is_error(status):
        return str(status.get("message") or "OpenAI span failed")
    return None


def _status_is_error(status: Any) -> bool:
    if not isinstance(status, Mapping):
        return False
    code = str(status.get("code") or status.get("status") or "").upper()
    return "ERROR" in code or code in {"FAILED", "FAILURE"}


def _duration(item: Mapping[str, Any]) -> tuple[float, float | None, float | None]:
    if item.get("duration_ms") is not None:
        try:
            return float(item["duration_ms"]), None, None
        except (TypeError, ValueError):
            pass
    start = _timestamp(item.get("started_at", item.get("start_time")))
    end = _timestamp(item.get("ended_at", item.get("end_time")))
    if start is None or end is None or end < start:
        return 0, start, end
    return (end - start) * 1000, start, end


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    if not value:
        return None
    text = str(value).strip().replace(" T", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def _usage(item: Mapping[str, Any], attributes: Mapping[str, Any]) -> tuple[int, int]:
    usage = _span_data(item).get("usage")
    usage = usage if isinstance(usage, Mapping) else {}
    input_value = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_value = usage.get("output_tokens", usage.get("completion_tokens"))
    if input_value is None:
        input_value = attributes.get("llm.token_count.prompt")
    if output_value is None:
        output_value = attributes.get("llm.token_count.completion")
    return _int(input_value), _int(output_value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _feedback(items: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    feedback: dict[str, Any] = {}
    for item in items:
        value = item.get("feedback")
        if isinstance(value, Mapping):
            feedback.update(value)
        for key, attribute in _attributes(item).items():
            if str(key).startswith("feedback."):
                feedback[str(key)[len("feedback.") :]] = attribute
    return feedback
