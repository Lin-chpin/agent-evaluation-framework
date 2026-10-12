# 工具行为预置与可选语义复核

工具预置复用 `NormalizedTrace`、`EvaluationEngine`、硬门禁和现有报告。语义预置复用已有 OpenAI-compatible reviewer，只产生人工复核建议。两者均按 Case 配置启用，不增加核心依赖。

## 工具行为配置

在 JSONL Case 的 `metadata` 中添加：

```json
{
  "behavior_evaluators": {
    "required_tools": ["search_docs"],
    "forbidden_tools": ["delete_records"],
    "tool_order": ["search_docs", "summarize"],
    "max_tool_calls": 3,
    "no_tool_errors": true
  }
}
```

| 配置 | 检查含义 |
| --- | --- |
| `required_tools` | 每个指定工具至少调用一次；不代表调用成功或参数正确。 |
| `forbidden_tools` | 不得出现指定工具调用。 |
| `tool_order` | 指定工具按 Trace 记录顺序形成有序子序列，允许中间穿插其他工具；重复名称要求重复调用。 |
| `max_tool_calls` | 单次运行中已标记工具调用的数量上限。 |
| `no_tool_errors` | 工具事件不得带有 `error`，或 `error`、`failed`、`failure` 状态。 |

只检查明确配置的项。不合法的配置会报错。配置后这些项是确定性硬门禁；`consistency_runs` / `consistency_check` 产生的每次运行都分别检查，任一次失败即阻断 Case。报告中的检查消息包含轮次及 Trace ID，调用列表或计数保存在 `actual` 中。

Adapter 必须把工具事件标记为：

```python
TraceEvent("CustomTool", "execute", fields={"kind": "tool", "tool_name": "search_docs"})
```

也识别 `module="Tool"` 或 `"OpenAITool"`（`action` 为工具名），以及 `span_type="function"` / `"mcp_tools"`。现有 OpenAI Trace 转换器和 BFCL Adapter 已符合该约定。普通 Agent 动作不计为工具调用。

若 Trace 提供 `fields.tool_call_count`，但计数与识别到的工具事件不一致，增加 `tool_trace_coverage` 硬失败，避免把未识别的调用当成零调用放行。没有完整 Trace 时，无法证明禁止工具从未被调用；采集完整性、调用顺序及外部副作用记录仍由 Adapter 负责。工具名检查也不能验证调用参数、业务效果或最终任务是否完成。

## 无模型示例

仓库示例默认只配置工具预置；Adapter 是确定性演示，不代表真实 Agent 的效果。

```powershell
agent-eval run `
  --adapter examples/preset_evaluators_adapter.py `
  --suite smoke `
  --cases examples/preset_evaluators_cases.jsonl
```

该命令不需要 API Key，也不调用模型。未配置预置的现有 Case 继续走原有规则。

## 按业务需求启用语义复核

只有需要问答或 RAG 语义检查时，才在业务 Case 的 `metadata` 中添加：

```json
{"semantic_evaluators": ["answer_relevance", "reference_correctness", "groundedness"]}
```

| 名称 | 输入证据 | 判断目标 |
| --- | --- | --- |
| `answer_relevance` | Case 的 `input.message` / `question` / `query`，或完整 input；最终回答 | 回答是否回应需求。 |
| `reference_correctness` | `expected.answer` 或 `expected.reference_answer`；最终回答 | 是否与参考答案的关键语义一致，允许改写。 |
| `groundedness` | `trace.fields.context` / `retrieved_context` 或 `input.context`；最终回答 | 回答中的事实是否得到提供的上下文支撑。 |

可只选其中一项。模型调用还需要显式传入 `--use-llm` 并配置已有的 `AGENT_EVAL_MODEL`、`AGENT_EVAL_BASE_URL`、`AGENT_EVAL_API_KEY`；不增加新的模型配置入口。每个已选预置独立请求一次；原有失败诊断或 few-shot 分析可能额外请求。语义预置检查主 Trace 的最终回答，不测量重复回答的语义稳定性。

未提供 reviewer 时结果为 `UNAVAILABLE`；缺少输入证据、无效响应、缺少非空文本理由或请求异常时为 `UNCERTAIN`。有效返回为 `PASS`、`FAIL`、`UNCERTAIN`，均带 `authoritative: false`。Case 配置错误仍会报错，不能用它替代评测结果。

结果保存在 `llm_review.semantic_evaluators`，后续诊断响应不能覆盖该字段。非 `PASS` 结果进入现有 Markdown 报告与 `review_queue.jsonl`，不改变 `hard_pass`、`soft_warning_count`、release 或演化门禁。配置了语义复核但尚未全部 `PASS` 的 Case 不导出为成功 few-shot 候选；候选仍需人工确认。

这些预置尚未经过真实业务数据校准，不能当作准确率或防幻觉保证。需要用人工裁定及留出样本验证后，再决定业务使用方式。请求会把选中的问题、回答、参考答案或上下文发送到配置端点；不可信内容和 Prompt Injection 可能影响模型判断，结果始终需要人工复核。这里的 `groundedness` 判断回答与上下文的关系，不是 Scope Audit 的 Git Diff 行证据定位。

## 验证

```powershell
python -m unittest discover -s tests -p test_preset_evaluators.py -v
python -m unittest discover -s tests -q
python scripts/verify_bfcl_adapter.py --mode self-check
```

语义单元测试使用 Stub，验证证据输入、结果处理和非权威边界；不代表真实模型判断准确。

## English

Tool presets are explicit, per-case deterministic hard gates configured under `metadata.behavior_evaluators`: `required_tools`, `forbidden_tools`, ordered-subsequence `tool_order`, `max_tool_calls`, and `no_tool_errors`. Every collected trial is checked independently; failures retain the trial number, Trace ID and observed calls/count. Custom adapters must tag tool events with `fields={"kind": "tool", "tool_name": "..."}` or use the recognized Tool/OpenAITool and function/mcp_tools conventions. OpenAI traces and the BFCL adapter support this convention. A declared `fields.tool_call_count` mismatch fails the `tool_trace_coverage` gate. Complete capture and meaningful event ordering remain the adapter's responsibility; tool names cannot prove parameter correctness or real-world outcomes.

The included JSONL example enables tool presets only and needs no model. Semantic presets (`answer_relevance`, `reference_correctness`, `groundedness`) require explicit `metadata.semantic_evaluators` selection plus `--use-llm` and the existing reviewer environment configuration. Each selected preset sends one request using the evidence listed above, evaluating the primary trace only. Missing reviewers yield `UNAVAILABLE`; missing evidence, invalid verdicts/reasons or request errors yield `UNCERTAIN`. Invalid case configuration is a configuration error.

Semantic results remain advisory in `llm_review.semantic_evaluators`; diagnosis responses cannot overwrite or create this reserved field. Non-PASS results enter the existing report and review queue without changing deterministic release/evolution gates. Cases with unconfirmed semantic results are excluded from positive few-shot candidates. Real business calibration and human adjudication are required; Stub tests do not establish model accuracy. Selected evidence is sent to the configured endpoint, and untrusted evidence may influence model judgments. This groundedness preset is unrelated to Git Diff evidence grounding in Scope Audit.
