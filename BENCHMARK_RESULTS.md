# 公开证据与 Benchmark 结果

[中文](BENCHMARK_RESULTS.md) | [English](BENCHMARK_RESULTS.en.md)

## 结论

当前证据支持以下结论：框架能够把 Agent / Skill 的候选修改放进受控闭环，自动执行失败诊断、候选生成、沙箱应用、改进集、回归集和留出集复测，并根据确定性门禁接受或回滚候选。

当前证据不支持“所有业务都能稳定自进化”“无需业务测试集即可优化”或“已经证明真实医疗效果”等结论。

## 无密钥可复现证据

运行以下命令不需要模型 API Key：

```powershell
python scripts/verify_evidence.py --output evidence/verified-results.json
```

该命令完成五项验证：

1. 运行全部自动化测试。
2. 运行确定性文本型演化，验证坏候选回滚、好候选接受。
3. 运行独立进程代码 Agent 演化，验证代码候选同样经过回归和留出门禁。
4. 运行模拟审核历史的评测 Skill 演化。
5. 运行用户审核合成样本的评测 Skill 演化，并输出一致率、误报、漏报和字段准确率。

GitHub Actions 会在 Windows 和 Linux 上执行同一命令，并上传机器可读 JSON。

本次本地 Python 3.12 结果保存在 [evidence/verified-results.json](evidence/verified-results.json)。验证脚本同时记录时间戳、框架版本、Git 提交、工作区是否有未提交改动、运行平台和复现命令。该证据文件直接记录其对应的精确提交和工作区状态，避免文档中的提交号随版本更新而失效。当前 `main` 的 GitHub Actions 继续在 Windows 和 Linux 上运行同一验证脚本并保存对应提交的产物。

每套数据保留两种身份。`source_file_sha256` 是原始 JSONL 文件的逐字节哈希，`normalized_case_manifest.sha256` 是框架解析并规范化 case 后的清单哈希。两者用途不同，数值不应相同。前者证明输入文件没有替换，后者证明恢复和候选比较使用相同的规范化测试内容。

## 已取得的结果

| 证据 | 结果 | 能证明什么 |
| --- | --- | --- |
| 自动化测试 | 42/42 通过 | 规则、演化决策、场景独立门禁、严格 holdout 门禁、多轮继续、冻结数据恢复校验、空集与重复 ID 拒绝、追加式恢复身份、预算、LLM JSON 边界、候选证据隔离、Gold 复核、评测指标、跨进程互斥、SQLite 多写者、多文件操作与回滚、容器安全默认值、进程超时和输出超限终止符合当前测试口径 |
| 确定性文本演化 | 坏候选 rollback，好候选 accept | 三数据集门禁能够阻止破坏留出集的候选，并保留安全改进 |
| 代码 Agent 演化 | 坏代码 rollback，好代码 accept；文本文件写入、删除和移动均进入候选目录 | 单文件和多文件目录候选能在独立工作区进入同一演化闭环，文件操作受路径和冲突校验，回滚不覆盖基线 |
| 评测 Skill 演化 | 坏候选 rollback，好候选 accept；improvement 0% → 100%，regression 100% → 100%，holdout 0% → 100% | 模拟审核报告可以按 improvement/regression/holdout 进入评测 Skill 自身的受控回放闭环 |
| 评测 Skill 人工审核演化 | 10/10 已审核；两个参考候选均 rollback；较好候选 improvement 0% → 100%，regression 100% → 100%，holdout 25% → 75% | 人工审核结果可以驱动修改和门禁；严格策略不会因候选已有明显改善而放过剩余 holdout 错误 |
| 评测 Skill 14B 重复评分 | 10 条 × 5 轮，50/50 调用成功；原 gold 一致率每轮 90%，重复稳定性 100%；复核 REVIEW-008 后事后重评分一致率 100% | 同一 Skill 与模型配置在本样本上输出稳定；Gold 复核与模型稳定性必须分开记录 |
| 评测 Skill 14B AI 演化 | AI 正确诊断 2 个 badcase，但生成与 baseline 相同的候选；三套数据均无改善，候选 reject | 不完美 Evolver 的无效修改不会因“由 AI 生成”而获得特殊待遇，最小改进门禁能够阻止无效晋级 |
| 私有外部系统 smoke | 20/20 通过，0 个硬失败，0 个软告警 | 框架能够接入实际 Agent 编排、原生评测入口和真实 Trace |
| 私有外部系统 Prompt 演化 | improvement 25% → 100%，regression 100% → 100%，holdout 75% → 100% | AI 能根据失败 Trace 生成一个通过既定门禁的 Prompt 候选 |
| 3 次独立重复 | 分别得到接受、外部生成失败和 holdout 回滚 | 外部 Evolver 的不同结果都进入同一审计、停止和门禁流程 |
| 私有外部系统可追溯重跑 | 运行前记录框架、目标、执行产物和数据集身份；第一个候选 rollback，第二个候选 accept | 参考结果绑定实际执行身份，并在同一次运行中保留回滚和接受决策 |
| 单机并发完整性 | 1、8、32 workers 各 1000 case，均为 1000 个唯一结果、0 个硬失败，10 次瞬态失败全部恢复 | 有界线程并发下未发现 case 丢失或重复，失败重试结果可审计 |
| 并发故障记账与恢复 | 5 个永久失败均被记录；500 → 1000 case 恢复后得到 1000 个唯一结果；两个进程写入 50 个不同运行无丢失 | 永久错误不会被吞掉，分段恢复和 SQLite 多写者保持记录完整性 |
| 真实容器 smoke | Linux CI 中工作区只读、根文件系统只读、网络禁用、`/tmp` 可写全部通过 | Docker 默认隔离已经由真实容器行为验证，不只验证命令字符串 |
| 短周期 soak | 10 秒、51 个连续批次、5100 个 case、0 失败；初始和结束线程数均为 1 | 重复运行期间结果完整，线程池在每批结束后回收 |

## 单机并发基线

Windows 11、Python 3.12.13 的无密钥合成压力结果保存在 [evidence/concurrency-results.json](evidence/concurrency-results.json)。每档运行 1000 个 case，并按固定规则注入 10 次首次调用失败；`retries=1` 后三个档位均得到 1000 个唯一结果和 0 个硬失败。

| Workers | 吞吐量 | P95 Agent 延迟 | 峰值 traced memory |
| ---: | ---: | ---: | ---: |
| 1 | 434.29 case/s | 1.64 ms | 9,975,748 bytes |
| 8 | 530.52 case/s | 2.21 ms | 10,055,181 bytes |
| 32 | 512.65 case/s | 2.67 ms | 10,163,531 bytes |

这组吞吐量是一次本机运行快照，会随调度和机器负载变化，不能作为生产容量指标。合成 Agent 每次只休眠约 1 毫秒，主要测量线程调度、规则计算和 SQLite 写入开销；8 workers 后已经进入本机开销饱和区，因此 32 workers 的瞬时吞吐没有继续上升。这不是稳定性失败：三个档位都得到 1000 个唯一结果、0 个硬失败，瞬态失败也全部恢复。加入 workers 两倍的有界在途窗口后，1000-case traced memory 保持在约 10 MB。当前证据支持单机有界并发下的共享状态完整性；真实 HTTP Agent 的容量上限仍需由接入方按自身延迟、限流和 SLO 执行 M4 压测。

短周期持续运行结果保存在 [evidence/soak-results.json](evidence/soak-results.json)。10 秒内连续完成 51 个批次和 5100 个 case，0 失败；初始和结束线程数均为 1，Python traced memory 峰值约 2.48 MB。

## 评测 Skill 专项证据

无密钥证据脚本新增 `evaluator_skill_evolution`。数据模拟未来的人工审核历史：1 条模拟判错报告进入 improvement，2 条模拟审核报告进入 regression，另有 1 条不暴露给候选生成器的模拟历史报告进入 holdout。它们使用合成 gold，尚未经过用户或领域专家审核，因此明确记录 `human_reviewed=false` 和 `review_status=simulated`。三套文件 SHA-256 写入 [evidence/verified-results.json](evidence/verified-results.json)。

基线只认识旧错误模式，在 improvement 和 holdout 上均为 0%。第一个候选用新规则替换旧规则，破坏原本正确的历史判断，因此被 regression 回滚；第二个候选保留旧规则并从 improvement 抽象新错误模式，最终三套数据均为 100% 并在沙箱接受。

这是对“历史报告回放机制”的确定性证明，不是真实人工审核质量证明。并发压力机制不依赖被测对象语义，因此没有复制一套评测 Skill 专属压力引擎；同一 `EvaluationEngine`、SQLite、run 锁和有界 workers 继续生效。

2026-08-31，用户逐条审核了 10 条通用合成案例。2 条错误或部分正确报告进入 improvement，4 条确认正确报告进入 regression，另外 2 条确认正确报告和 2 条部分正确报告冻结为 holdout。`PARTIALLY_CORRECT` 保留为原始人工结论，但在二元发布硬门禁中映射为 `INCORRECT`，避免把不完整评测当作通过。原始审核见 [evidence/评测Skill人工审核表.md](evidence/评测Skill人工审核表.md)，机器样本见 `examples/evaluator_skill_human.*.jsonl`，数量与 SHA-256 已写入 [evidence/verified-results.json](evidence/verified-results.json)。

REVIEW-007 和 REVIEW-008 都经历了“首次判断为正确、复核后裁决为部分正确”。首次判断、复核结论和最终二值门禁均被保留。这两条样本证明业务方提供的 gold 也可能暂时有歧义；框架新增 `UNRESOLVED` 待审核状态，未裁决样本不得进入候选生成、回归、holdout 或公开准确率分母。

这 10 条数据已经进入独立的评测 Skill 演化参考任务。最新 gold 下，基线在 improvement、regression、holdout 上分别为 0%、100%、25%。把所有“通过”报告一律拒绝的过度修正候选破坏了 regression；检查部分决定性事实的候选达到 100%、100%、75%，但仍漏掉 REVIEW-008。评测 Skill 专用策略现在要求 holdout 全部通过，因此两个候选均被回滚，现用版本没有替换。确定性参考候选由作者预先编写，只证明门禁机制，不作为未见样本泛化证据。

独立稳定性实验冻结一个人工编写的通用评测 Skill、`Qwen/Qwen3-14B`、温度 0 和 128 输出 token 上限，在完整 10 条数据上运行 5 轮。50 次调用全部成功，总用量 11,969 tokens；所有案例五轮判定一致，重复稳定性为 100%。实验时 REVIEW-008 的人工 gold 仍为 `CORRECT`，所以每轮一致率为 90%。模型五轮都指出评测报告把“没有发生数据丢失”改写成“没有记录数据丢失”；业务方复核后将其裁决为 `PARTIALLY_CORRECT`，二值门禁为 `INCORRECT`。不改动模型输出的事后重评分得到五轮 100% 一致率。原始结果见 [evidence/evaluator-skill-stability-results.json](evidence/evaluator-skill-stability-results.json)，裁决后重评分见 [evidence/evaluator-skill-stability-adjudicated.json](evidence/evaluator-skill-stability-adjudicated.json)。该实验测量固定 Skill 的重复评分，不是候选生成或真实业务泛化实验。

本次候选由确定性参考生成器提供，用于验证人工审核数据确实参与修改、回归与留出门禁。它不等同于 AI 自主发现了这些规则，也不证明 10 条合成案例足以代表真实业务分布。

私有外部参考系统的聚合结果见 [首次演示摘要](evidence/ai-health-prompt-evolution-summary.json)、[三次重复汇总](evidence/ai-health-repeat-results.json)和[可追溯重跑摘要](evidence/ai-health-provenance-rerun-summary.json)。源码、Prompt、业务 case、适配器和内部报告不随本仓库公开。

## 数据与实验边界

- 私有外部参考系统使用合成 case，不是真实业务数据或生产 badcase。
- 评测 Skill 专项数据也是合成人工审核历史，不代表已经积累真实审核报告。
- 私有外部实验属于真实系统集成验证，不属于业务效果验证，也不用于推断其他系统的成功率。
- AI 候选生成具有随机性；框架通过结构边界、预算、最小改进、regression 和 holdout 门禁处理外部 Evolver 结果。
- 多文件快照与容器 Runner 已实现，真实 Docker smoke 已进入 Linux CI；构建缓存和多小时 soak 仍按实际需求扩展。

## 模型选择说明

AI 参与的机制实验有意使用 `Qwen/Qwen3-14B`。本项目验证的是候选修改能否被隔离、复测、接受或回滚，不是比较基础模型能力。较小模型产生的无效输出或错误修改属于框架需要处理的正常输入，用于验证结构边界、回归保护和 holdout 门禁。

14B 的结果不代表模型能力上限，也不与不同任务上的大模型结果合并计算成功率。更强模型可能提高候选产出效率，但不会改变硬门禁、人工 gold 和数据集隔离要求。若未来比较模型大小，将另建同任务、同 baseline、同冻结数据、同预算和同重复次数的对照实验，不追溯改写当前证据。

2026-08-31 的评测 Skill AI 演化实验中，14B 正确诊断了 improvement 中的两个错误判断，但候选生成阶段原样返回了旧版 `MODE=constant-correct`，没有形成有效修改。improvement 保持 0%，regression 保持 100%，历史冻结 holdout 保持 75%，候选因未达到最小改进被拒绝，baseline 未被覆盖。实验后 REVIEW-007 与 REVIEW-008 的 gold 经复核发生变化，当前 holdout baseline 为 25%；旧数据快照没有被覆盖。精简证据见 [evidence/evaluator-skill-ai-14b-summary.json](evidence/evaluator-skill-ai-14b-summary.json)，可公开审计见 [evidence/evaluator-skill-ai-14b-audit.json](evidence/evaluator-skill-ai-14b-audit.json)。该结果验证失败路径，不是 Skill 已改善的证据。

## 证据分层

### 框架机制

由无密钥确定性流程和自动化测试证明。任何贡献者都可以在本地和 CI 中复现。

### 私有真实系统集成

聚合结果证明框架曾通过真实接口、版本切换和 Trace 接入一个外部 Agent 系统。该项目不随本仓库公开，因此这些结果只作为补充集成记录，不承担公共可复现机制证据，也不用于推断其他业务系统的效果。

### 业务效果

尚未证明。它需要真实业务测试集、专家口径、历史 badcase 或线上反馈，不能由框架自身生成。

## 可公开使用的表述

> 在业务方提供测试集、评价规则和改动边界的前提下，本框架为 Agent / Skill 提供受控、可审计、可回退的自动评测与候选演化闭环。

## 不应使用的表述

- 所有 Agent 都能自动变好。
- AI 可以自行定义业务正确答案。
- 没有测试集也能实现自进化。
- 候选可以不经门禁直接发布生产。
- 当前实验已经证明真实医疗收益。
