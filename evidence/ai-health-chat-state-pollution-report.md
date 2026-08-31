# Agent Evaluation Report, ai-health-fresh-smoke-20260830

- Suite `smoke`
- Source `online`
- Status **failed**
- Cases 20
- Hard failures 7
- Soft or review warnings 8

## Suspected modules

- `ChatService` 7
- `PlannerAgentWrapper` 7
- `QualityCheckAgent` 7
- `IntentAnalyzer` 4

## Scenario statistics

- `boundary` 4/4, 95% CI 51.0% to 100.0%
- `emotional` 2/2, 95% CI 34.2% to 100.0%
- `medical` 1/4, 95% CI 4.6% to 69.9%
- `memory` 0/2, 95% CI 0.0% to 65.8%
- `mixed` 2/4, 95% CI 15.0% to 85.0%
- `risk` 4/4, 95% CI 51.0% to 100.0%

Failed or review-candidate cases were HAT-S01, HAT-S02, HAT-S03, HAT-S04, HAT-S09, HAT-S11, HAT-S13, and HAT-S14. The run used the single-case `/chat` path without native suite state cleanup. It is retained as integration-defect evidence and is not a valid business baseline.
