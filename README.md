# Agent Evaluation Framework

[中文](README.md) | [English](README.en.md)

一个与领域解耦的 Agent 自动评测、诊断、人工审核、回归和版本演化框架。框架不内置任何业务测试集，也不会绕过业务门禁直接修改或发布 Agent、Skill、Prompt 或安全规则。

公开测试结果、可支持的结论和一键复现方式见 [BENCHMARK_RESULTS.md](BENCHMARK_RESULTS.md)。

## 使用许可

本项目源码公开，供非商业研究、学习、测试、修改和分发使用，不属于 OSI 定义的开源软件。分发原项目、部分代码或衍生作品时，必须保留 [PolyForm Noncommercial License 1.0.0](LICENSE) 及其中的 `Required Notice`。商业使用需要取得版权所有者的书面授权，联系邮箱为 [linandchpin.2033@gmail.com](mailto:linandchpin.2033@gmail.com)。

Copyright © 2026 Lin-chpin。

## 领域项目只提供四项能力

领域项目通过一个 Python 适配文件导出 `ADAPTER = ProjectAdapter(...)`：

1. `call_agent(case, context)`：通过 HTTP、SDK、子进程或本地函数调用 Agent。
2. `read_trace(handle, case)`：把原生 Trace 转换成 `NormalizedTrace`。
3. `hard_gates`：发布阻断规则。
4. `soft_quality`：只产生质量告警和人工复核候选的规则。

测试集通过 `--cases`、`--regression`、`--smoke` 和 `--full` 外部挂载。仓库中的 `examples/cases.example.jsonl` 只用于演示，不会被框架默认加载。

版本演化使用基线适配器和候选适配器运行同一批 improvement、regression 和 holdout 数据。候选可以来自人工、外部 Agent 或任何生成流程，框架统一负责版本识别、效果比较、门禁决策和审计记录。

`evolve-auto` 在此基础上增加失败诊断、文本候选生成、沙箱应用和有限轮次自动迭代。领域项目仍然提供测试集、评价规则和目标适配器。

## 解耦边界

```text
领域适配层：Agent 调用、原生 Trace 转换、硬门禁、软质量
        ↓
核心评测层：结构、行为、一致性、隐式反馈
        ↓
流程编排层：run / regression → smoke → full
演化编排层：baseline ↔ candidate → accept / reject / rollback
        ↓
外围能力：SQLite、Markdown/JSON、人工审核、可选 LLM、few-shot 候选
```

- 核心规则不依赖 CLI、SQLite 或 LLM。
- LLM 只生成语义分析、疑似模块、修改建议和 few-shot 候选，不参与硬门禁判定。
- 人工最终结论与自动报告分开保存。
- 在线 fresh run 用于验证当前 Agent；离线 Trace 只用于重新计算评分和诊断。
- 框架把 `timeout_seconds` 传给 `call_agent`；HTTP、SDK 或子进程适配器负责真正取消超时调用。需要强隔离时再把 Runner 放入独立进程或容器。

## 标准 Trace

`read_trace` 应返回 `NormalizedTrace`，其中：

- `trace_id` 和 `final_output` 是框架完整性字段。
- `events` 保存 `module`、`action`、`status`、`duration_ms` 和 `error`，供行为异常和模块定位使用。
- `fields` 保存领域项目希望用规则检查的结构化结果。
- `feedback` 可保存 `explicit_negative`、`repeated_question`、`rephrased` 等弱信号。
- `target_type`、`target_id`、`target_version` 用于区分 Agent、普通 Skill 和测评 Skill。

字段命名和业务含义由项目适配层决定，框架只读取路径和规则。

## 四层评测

1. **结构层**：Trace、最终输出及项目声明的硬门禁和软质量字段。
2. **行为层**：步骤数、重试、延迟、重复 module/action 和模块错误。
3. **一致性层**：仅对 case 中启用 `consistency_check` 的高价值节点执行多次运行；差异只进入人工复核候选。
4. **反馈层**：隐式反馈只生成候选，不自动判定 badcase。

## 安装与运行

业务项目准备接入、验收或上线时，先按[业务接入验收协议](功能列表/业务接入验收.md)冻结版本、数据集、Gold、门禁、SLO 和回滚目标。只有合成数据时不能声明真实业务效果或生产验收。

```powershell
cd path\to\agent-evaluation-framework
python -m pip install -e .

agent-eval run `
  --adapter examples/project_adapter.py `
  --suite smoke `
  --cases examples/cases.example.jsonl `
  --collect-few-shot
```

生产发布评测固定按照针对性 regression、核心 smoke、可选 full 执行。任一阶段出现硬门禁失败，后续阶段停止：

```powershell
agent-eval release `
  --adapter path/to/project_adapter.py `
  --regression path/to/regression.jsonl `
  --smoke path/to/smoke.jsonl `
  --full path/to/full.jsonl
```

每条结果会立即写入 SQLite。使用相同 `--run-id --resume` 可以跳过已经完成的 case。

## 通用版本演化

`evolve` 同时验证基线版本和候选版本。三个数据集职责固定，内容和业务口径由领域项目提供：

- `improvement`：候选版本声称要改善的问题。
- `regression`：已经确认、不能复发的历史能力。
- `holdout`：候选生成过程没有使用的留出样本。

```powershell
agent-eval evolve `
  --baseline-adapter examples/evolution_baseline_adapter.py `
  --candidate-adapter examples/evolution_candidate_adapter.py `
  --candidate examples/evolution.candidate.json `
  --policy examples/evolution.policy.json `
  --improvement examples/evolution.improvement.jsonl `
  --regression examples/evolution.regression.jsonl `
  --holdout examples/evolution.holdout.jsonl `
  --experiment-id example-evolution
```

候选清单记录目标类型、目标 ID、基线版本、候选版本、变更类型和产物位置。`change_type` 可以表示 `prompt`、`skill`、`few-shot`、`tool-policy`、`rag-config`、`output-schema` 或领域自定义类型。

策略文件可以约束 regression、holdout 和数值目标。目标可以直接使用 `hard_pass`、`soft_warning_count`、`latency_ms`、`steps`，也可以从标准结果路径读取业务字段；支持 `mean`、`sum`、`min` 和 `max` 聚合，以及最大允许退化和最低改善幅度。

`scenario_gates` 可以为高风险或小样本场景单独设置最低样本数、最低通过率和允许退化。未配置的场景继续服从整体门禁，已配置场景不会被全量平均值掩盖。示例见 [场景门禁策略](examples/evolution.scenario-policy.example.json)。

决策含义：

- `accept`：目标问题获得可测量改善，且保护集没有超过策略允许的退化。
- `reject`：候选没有达到声明的改善目标。
- `rollback`：候选破坏 regression、holdout、版本身份或必要指标。

每次演化会保存完整的基线与候选运行结果、`evolution.json`、`evolution_report.md` 和 SQLite 审计记录。框架不替业务决定正确答案，也不直接部署候选版本；外部 Agent 可以生成候选，框架负责决定它是否有资格进入下一阶段。

## 文本型自动演化

自动演化适配器导出 `AUTO_EVOLUTION = AutoEvolutionAdapter(...)`，并提供基线文本产物、诊断器、候选生成器和根据产物创建运行适配器的函数。

仓库中的确定性示例会先生成一个破坏留出集的候选，再生成一个保留基线能力的候选。框架应当回滚前者并接受后者。

```powershell
agent-eval evolve-auto `
  --auto-adapter examples/auto_evolution_adapter.py `
  --policy examples/evolution.policy.json `
  --improvement examples/evolution.improvement.jsonl `
  --regression examples/evolution.regression.jsonl `
  --holdout examples/evolution.holdout.jsonl `
  --max-rounds 1 `
  --max-candidates-per-round 2 `
  --max-elapsed-seconds 300 `
  --max-evolver-calls 4 `
  --loop-id router-evolution-v1
```

`TextArtifactWorkspace` 只在 `.agent-eval/workspaces` 中保存基线快照和候选产物，不覆盖领域项目原文件。接受结果仍然停留在沙箱，生产发布不属于自动循环。

循环会原子写入 `.agent-eval/workspaces/<loop-id>/checkpoint.json`。异常、时间预算或生成调用预算中止后，可以提高预算或修复外部故障，再使用相同参数追加 `--resume`；已完成的 case 从 SQLite 读取，内容相同的已暂存候选会直接复用。时间预算在阶段边界检查，不会强杀正在执行的领域调用；单次调用仍由 `--timeout` 和领域适配器负责。

`--max-evolver-calls` 统计诊断器和候选生成器的调用次数。它不冒充被测 Agent 的 token 或供应商账单；业务 Agent 的模型成本应通过 Trace 自定义指标进入评测策略。

### 代码型 Agent

代码候选可以把 `change_type` 设为 `code`，并由领域适配器使用 `run_agent_process` 启动。候选文件仍由 `TextArtifactWorkspace` 放进独立目录，进程以该目录为工作目录运行，超时会终止进程树并保留 stdout/stderr。

```powershell
agent-eval evolve-auto `
  --auto-adapter examples/code_auto_evolution_adapter.py `
  --policy examples/evolution.policy.json `
  --improvement examples/evolution.improvement.jsonl `
  --regression examples/evolution.regression.jsonl `
  --holdout examples/evolution.holdout.jsonl `
  --max-rounds 1 `
  --max-candidates-per-round 2
```

该能力隔离候选文件和进程生命周期，不是防恶意代码的安全沙箱。需要执行不可信代码时，适配器必须放进容器、虚拟机或受限执行服务。

需要模型自动诊断和生成候选时，可以在领域适配器中使用 `OpenAICompatibleTextEvolver` 的 `diagnose` 和 `generate_candidates` 方法。它复用 `AGENT_EVAL_MODEL`、`AGENT_EVAL_BASE_URL` 和 `AGENT_EVAL_API_KEY`，只接收改进集失败证据和当前文本，不接收回归集或留出集内容。最终接受与回滚仍由确定性门禁决定。

## 参考系统集成

`integrations/ai_health_assistant/` 提供健康助手的独立领域适配器、case 转换器和隔离 Prompt 运行时。普通评测默认通过健康助手原生 `/evaluate` 隔离执行整套用例，再从 `/traces` 按 `caseId` 读取真实轨迹；`/chat` 模式只用于单例调试。详细命令和当前边界见 [集成说明](integrations/ai_health_assistant/README.md)。

Prompt 自动演化会复制 case 和版本文件到框架沙箱，在随机端口启动独立 JAR，注册并激活候选后执行 improvement、regression 和 holdout。2026-08-30 的机制测试中，AI 候选把三个数据集的硬通过率分别从 25%、100%、75% 提升或保持到 100%、100%、100%。候选只在沙箱中接受，健康项目文件和生产发布不在自动循环的写入范围内。

## Case 格式

```json
{
  "id": "CASE-001",
  "scenario": "routing",
  "input": {"message": "..."},
  "expected": {"route": "TARGET", "keyword": "..."},
  "metadata": {
    "consistency_check": true,
    "consistency_runs": 2,
    "system_constraints": {
      "max_steps": 12,
      "max_retries": 1,
      "max_latency_ms": 30000
    }
  }
}
```

期望字段和规则由项目适配器对应。框架没有医疗、路由、RAG 或工具调用等预设语义。

## 人工审核和自进化产物

每次运行生成：

- `results.json`：逐 case 事实、规则结果和 Trace。
- `report.md`：硬失败、软告警、场景分布和疑似模块。
- `scenario_stats.json`：按场景及 `targetType/targetId` 统计样本量、通过率和 95% 置信区间。
- `review_queue.jsonl`：等待人工确认的报告。
- `few_shot_candidates.jsonl`：通过全部门禁和质量检查的成功路径候选。

每次 `evolve` 额外生成：

- `evolution.json`：候选、策略、三类数据集结果和最终决策。
- `evolution_report.md`：基线与候选的通过率、告警、延迟、自定义目标和版本链路。

每次 `evolve-auto` 额外生成：

- `auto_evolution.json`：每轮诊断、候选、评测结果和最终沙箱版本。
- `auto_evolution_report.md`：候选接受、拒绝、回滚和停止原因。

人工最终结论写入 SQLite：

```powershell
agent-eval review `
  --run-id RUN_ID `
  --case-id CASE_ID `
  --decision confirmed_badcase `
  --conclusion "人工确认的最终原因"
```

确认的 badcase 由维护者写回领域项目的 regression 测试集；few-shot 候选经脱敏、去重和人工审核后再绑定对应 Skill 版本。框架只产出候选，不自动改动领域项目。

人工确认后可以导出，不直接覆盖领域项目文件：

```powershell
agent-eval export --kind regression --output regression.candidates.jsonl
agent-eval export --kind few-shot --output few-shot.accepted.jsonl
```

领域项目维护者决定何时合并导出结果、创建新版本并执行回归。

评测 Skill 的人工审核样本使用独立晋级入口。未能确认的业务 gold 必须留在 `pending`，不能进入正式门禁：

```powershell
agent-eval promote-review `
  --input review-sample.json `
  --outcome UNRESOLVED `
  --role pending `
  --conclusion "业务方暂时无法裁决" `
  --reviewer "business-owner" `
  --output pending-reviews.jsonl
```

复核后可将同一记录晋级到 improvement、regression 或 holdout；`review_history` 会保留首次判断与最终裁决。

## 可选 LLM

不配置 LLM 时，确定性评测、报告、人工审核和回归流程仍可运行。需要语义分析时设置：

```powershell
$env:AGENT_EVAL_MODEL = "your-model"
$env:AGENT_EVAL_BASE_URL = "https://provider.example/v1"
$env:AGENT_EVAL_API_KEY = "..."
```

运行时增加 `--use-llm`。LLM 输出始终标记为非权威建议。

## 验证

```powershell
python -m unittest discover -s tests -v
```

参与开发前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)；安全问题请按照 [SECURITY.md](SECURITY.md) 私下报告。
