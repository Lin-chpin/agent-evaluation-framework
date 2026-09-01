# 测试集自动选择

`agent-eval select-tests` 读取领域项目相对指定 Git 基线的已跟踪改动，并纳入未跟踪的 UTF-8 文本文件，然后输出推荐模式、实际应跑的套件、规则与 AI 判断以及是否需要人工复核。

## 三层决策

1. 规则识别文档、UI、报告、Prompt、Planner、安全、RAG、检索和结果结构等改动，给出最低测试强度。
2. 可选 AI 判断改动的真实影响范围和风险。AI 可以升级测试强度，不能降低规则底线。
3. AI 置信度低于阈值、AI 与规则结论不同或任一层认为改动高风险时，结果标记 `human_review_required`。框架不会替人自动确认。

模式按测试强度累计执行。`smoke` 只建议 smoke；`regression` 建议 regression 和 smoke；`full` 建议 regression、smoke 和 full。

## 本地模型与第三方 API

本地部署模型的请求不离开用户设备，因此默认读取完整 diff，不做脱敏。`local` 模式只接受 `localhost`、回环地址、私有 IP 或 `.local` 地址，避免把公网服务误标成本地模型。若本地服务仍会转发请求，接入方应改用 `remote` 模式或显式使用 `--ai-input summary`。

第三方 API 的 `remote` 模式强制使用脱敏摘要，并拒绝发送 raw diff。摘要只保留文件数量、增删行数、扩展名分布、文件类别和通用影响信号，不包含源码、文件路径、URL、密钥或其他具体值。

模型配置复用 `AGENT_EVAL_MODEL`、`AGENT_EVAL_BASE_URL`、`AGENT_EVAL_API_KEY` 和现有 OpenAI-compatible 接口。未配置 AI 时，规则层仍可独立运行，不产生模型费用。

## 输出和退出状态

命令向标准输出打印 JSON，也可以用 `--output` 保存。无需人工复核时退出码为 0；需要人工复核时退出码为 2，便于 CI 暂停并交给审核人。
