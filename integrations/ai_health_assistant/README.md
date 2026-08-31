# ai-health-assistant 集成

本目录把外部 `ai-health-assistant` 项目的 HTTP Agent 和 Trace 接入通用评测框架。集成代码独立于核心包，不修改健康助手源码，也不把医疗规则写入通用层。

## 当前范围

- `adapter.py` 默认调用健康助手原生 `POST /evaluate?mode={suite}`，再读取 `GET /traces` 并按 `caseId` 映射。原生评测会在每个独立 case 前清空对话状态。
- 设置 `AI_HEALTH_EXECUTION_MODE=chat` 时可调用 `POST /chat`，随后读取 `GET /trace/{traceId}`；该模式只适合单例调试，不保证测试用例隔离。
- `convert_cases.py` 把健康助手现有 JSONL case 转换为通用 `EvalCase` 格式。
- `isolated_runtime.py` 为每次机制测试复制 case 和 Prompt 版本，在随机端口启动独立 JAR，注册并激活候选，然后运行原生 smoke；进程退出后候选状态随沙箱一起隔离。
- `prompt_auto_evolution.py` 把 PLANNER Prompt 接入通用诊断、候选生成和三数据集门禁。
- 路由、路径、追问、安全、禁用内容、质检错误和编排错误作为硬门禁。
- `must_include` 只产生软质量告警。
- 支持读取历史 Trace 做离线契约检查，但运行时修改必须重新在线执行。

自动演化只操作框架目录下的隔离副本。健康项目的 `prompt_versions.json` 和生产实例不会被注册、激活或回滚操作改写。复制时会移除当前 JAR 无法反序列化的派生字段 `versionId`，但保留版本内容和激活版本；兼容处理只存在于本集成层。

## 已验证运行

2026-08-30 首次使用 `/chat` 逐条运行 20 条 smoke，得到 13 条通过硬门禁、7 条硬失败和 8 条软告警。复核后确认该运行绕过了健康助手原生评测的 case 状态清理，因此只保留为集成缺陷证据，不作为有效业务基线。

污染状态触发 `SkipFollowUpAgent` 后，它把空模型名传入 `LlmService`，最终请求包含必填字段 `"model": null`，造成 17 次 HTTP 400。适配器现已改用原生批量评测入口，重新建立隔离基线后才判断是否还需修改参考系统。

同日使用原生 `/evaluate` 复跑后，20 条用例全部通过，0 个硬失败、0 个软告警，20 条 Trace 均带独立 `caseId`，运行期间无 HTTP 400 或超时。脱敏报告见 [原生 smoke 报告](../../evidence/ai-health-native-smoke-report.md)。

首次非隔离运行的脱敏报告见 [状态污染报告](../../evidence/ai-health-chat-state-pollution-report.md)。两次运行都没有写入健康助手仓库。

## 隔离 Prompt 自演化验证

冻结的 20 条用例被拆为 4 条 improvement、8 条 regression 和 8 条 holdout。受控缺陷把普通请求错误路由到 EMOTIONAL，用于验证机制，不表示健康项目曾在生产中出现该故障。

2026-08-30 的端到端运行完成了以下闭环：

- 缺陷基线：improvement 1/4、regression 8/8、holdout 6/8。
- AI 诊断引用 HAT-S01、HAT-S02、HAT-S04 的失败 Trace，定位 PLANNER 路由问题。
- AI 生成 1 个完整 Prompt 候选；隔离实例注册为新版本并激活。
- 候选结果：improvement 4/4、regression 8/8、holdout 8/8，三个集合均为 0 个软告警。
- 门禁接受版本 `ai-r1-c1-92f1e5`，接受结果停留在沙箱，不发布到健康项目。

公开摘要见 [AI Prompt 演化摘要](../../evidence/ai-health-prompt-evolution-summary.json)。候选在 improvement 和 holdout 上的平均延迟分别增加约 3.36 秒和 2.19 秒；本次机制测试没有业务延迟阈值，因此延迟只记录、不参与阻断。

该结果证明框架能支撑并约束一次真实 Agent 系统上的 Prompt 演化闭环，不证明医疗建议已获得真实业务数据或专家标注验证。

## 公开复现范围

历史实验保存了数据集哈希、候选、指标、决策和本机完整审计哈希，但运行前没有记录健康助手源码修订和 JAR SHA-256。完整审计也没有作为公开文件提交。因此，公开材料可以核对实验摘要和不利结果，不能逐位重建 2026-08-30 的目标系统。项目不会用今天的源码或 JAR 身份追溯填补这个缺口。

新的集成实验应在运行前记录外部项目提交、工作区状态、JAR SHA-256、Java 版本、模型配置和数据集哈希。以下命令展示同一接入流程，不保证随机模型生成相同候选。

```powershell
$env:AI_HEALTH_PROJECT_ROOT = "C:\path\to\ai-health-assistant"
$env:AI_HEALTH_JAR = "C:\path\to\ai-health-assistant\target\healthai-1.0.0.jar"
$env:AI_HEALTH_JAVA = "java"
$env:AGENT_EVAL_MODEL = "Qwen/Qwen3-14B"
$env:AGENT_EVAL_BASE_URL = "https://provider.example/v1"
$env:AGENT_EVAL_API_KEY = "..."

agent-eval evolve-auto `
  --auto-adapter integrations/ai_health_assistant/prompt_auto_evolution.py `
  --improvement integrations/ai_health_assistant/benchmarks/improvement.jsonl `
  --regression integrations/ai_health_assistant/benchmarks/regression.jsonl `
  --holdout integrations/ai_health_assistant/benchmarks/holdout.jsonl `
  --max-rounds 2 `
  --max-candidates-per-round 2 `
  --timeout 900 `
  --loop-id ai-health-planner-evolution
```

历史运行使用默认演化门禁，没有配置后来新增的 `scenario_gates`。它不能追溯声明为场景门禁实验。新业务接入可以根据冻结口径单独配置高风险场景。

## 转换用例

```powershell
python integrations/ai_health_assistant/convert_cases.py `
  C:\path\to\ai-health-assistant\.agents\skills\health-agent-test\assets\cases\smoke.jsonl `
  .agent-eval/integrations/ai-health-assistant-smoke.jsonl
```

## 在线运行

先在健康助手项目中启动服务，再运行

```powershell
$env:AI_HEALTH_BASE_URL = "http://127.0.0.1:8080"
$env:AI_HEALTH_TARGET_VERSION = "current"
$env:AI_HEALTH_EXECUTION_MODE = "suite"

agent-eval run `
  --adapter integrations/ai_health_assistant/adapter.py `
  --suite smoke `
  --cases .agent-eval/integrations/ai-health-assistant-smoke.jsonl `
  --timeout 60
```

这里验证的是框架与实际 Agent 系统的接口和 Trace 集成，不代表医疗业务效果已经获得真实数据验证。
