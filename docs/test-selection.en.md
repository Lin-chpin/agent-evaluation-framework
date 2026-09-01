# Automatic test selection

`agent-eval select-tests` reads tracked changes against a selected Git base and includes untracked UTF-8 text files. It returns the recommended mode, suites to run, rule and AI assessments, and whether human review is required.

## Three decision layers

1. Deterministic rules recognize documentation, UI, reporting, Prompt, Planner, safety, RAG, retrieval, and result-schema changes and establish the minimum test strength.
2. An optional AI estimates the change's real impact and risk. It may upgrade the mode but cannot lower the rule floor.
3. The result sets `human_review_required` when AI confidence is below the threshold, AI and rules disagree, or either layer marks the change as high risk. The framework does not approve the change for the reviewer.

Modes are cumulative. `smoke` recommends smoke only; `regression` recommends regression and smoke; `full` recommends regression, smoke, and full.

## Local models and third-party APIs

A locally deployed model keeps the request on the user's device, so local mode reads the complete diff by default without redaction. It accepts only `localhost`, loopback addresses, private IPs, or `.local` endpoints to prevent a public service from being mislabeled as local. If a local gateway forwards requests elsewhere, use remote mode or explicitly select `--ai-input summary`.

Remote mode forces a sanitized summary and rejects raw diffs. The summary contains only file counts, changed-line counts, extension distribution, file categories, and generic impact signals. It excludes source text, file paths, URLs, secrets, and concrete values.

Model configuration reuses `AGENT_EVAL_MODEL`, `AGENT_EVAL_BASE_URL`, `AGENT_EVAL_API_KEY`, and the existing OpenAI-compatible interface. With AI disabled, the rule layer works independently and incurs no model cost.

## Output and exit status

The command prints JSON and can also save it with `--output`. It exits with 0 when no review is needed and 2 when CI should pause for a reviewer.
