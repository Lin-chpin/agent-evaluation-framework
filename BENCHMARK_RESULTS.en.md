# Published Evidence and Benchmark Results

[中文](BENCHMARK_RESULTS.md) | [English](BENCHMARK_RESULTS.en.md)

## Conclusion

The current evidence supports one bounded claim. The framework can place Agent and Skill changes inside a controlled loop that diagnoses failures, generates candidates, applies them in an isolated workspace, reruns improvement, regression, and holdout sets, then accepts or rolls back each candidate through deterministic gates.

The evidence does not show that every business can evolve reliably, that optimization works without a business test set, or that the reference experiments prove real clinical outcomes.

## Reproducible evidence without an API key

This command requires no model API key.

```powershell
python scripts/verify_evidence.py --output evidence/verified-results.json
```

It performs five checks.

1. Runs the complete automated test suite.
2. Runs deterministic text evolution and verifies rollback of a harmful candidate and acceptance of a safe candidate.
3. Runs a code Agent in an independent process and verifies that code candidates pass through the same regression and holdout gates.
4. Replays evaluator-Skill evolution with simulated review history.
5. Replays evaluator-Skill evolution with user-reviewed synthetic samples and reports agreement, false positives, false negatives, and field accuracy.

GitHub Actions runs the same command on Windows and Linux and uploads the machine-readable JSON result.

The local Python 3.12 result is stored in [evidence/verified-results.json](evidence/verified-results.json). The script records the timestamp, framework version, Git revision, dirty-worktree state, runtime platform, and reproduction command. The evidence file itself records the exact revision and worktree state it belongs to, so commit identifiers do not go stale in the surrounding documentation. GitHub Actions on the current `main` branch continues to run the same script on Windows and Linux and stores artifacts for the corresponding revision.

Each dataset has two identities. `source_file_sha256` is the byte-level hash of the original JSONL file. `normalized_case_manifest.sha256` hashes the cases after framework parsing and normalization. The values serve different purposes and are not expected to match. The first detects replacement of the input file, while the second binds resume and candidate comparison to the same normalized test content.

## Results

| Evidence | Result | What it supports |
| --- | --- | --- |
| Automated tests | 33/33 passed | Current tests cover rules, evolution decisions, independent scenario gates, strict holdout gating, continued multi-round improvement, frozen-dataset resume checks, budgets, recovery, LLM JSON boundaries, candidate-evidence isolation, Gold adjudication, evaluator metrics, cross-process locking, concurrent SQLite writers, multi-file rollback, container security defaults, and process timeout behavior. |
| Deterministic text evolution | Harmful candidate rolled back; safe candidate accepted | Three-set gating blocks a candidate that damages holdout while preserving a safe improvement. |
| Code Agent evolution | Harmful code rolled back; safe code accepted | Single-file and multi-file directory candidates can enter the same loop without rollback overwriting the baseline. |
| Evaluator-Skill evolution | Harmful candidate rolled back; safe candidate accepted. Improvement 0% to 100%, regression 100% to 100%, holdout 0% to 100%. | Simulated review reports can drive a controlled evaluator-Skill replay across improvement, regression, and holdout. |
| Human-reviewed evaluator-Skill evolution | 10/10 reviewed; both reference candidates rolled back. Better candidate moved improvement 0% to 100%, regression 100% to 100%, holdout 25% to 75%. | Human decisions can drive changes and gates. A strict policy still blocks a clearly improved candidate while any holdout error remains. |
| Repeated 14B evaluator-Skill scoring | 10 cases across 5 rounds; 50/50 calls succeeded. Original-Gold agreement was 90% per round and repeat stability was 100%. Agreement became 100% after REVIEW-008 adjudication. | The fixed Skill and model configuration was stable on this sample. Gold adjudication and model stability must be reported separately. |
| 14B AI evaluator-Skill evolution | The AI diagnosed two bad cases correctly but returned a candidate identical to the baseline. All three sets remained unchanged and the candidate was rejected. | An ineffective AI-generated change receives no special treatment. The minimum-improvement gate blocks it. |
| Private external-system smoke suite | 20/20 passed with no hard failure or soft warning | The framework can connect to a real Agent orchestrator, its native evaluation endpoint, and real traces. |
| Private external-system Prompt evolution | Improvement 25% to 100%, regression 100% to 100%, holdout 75% to 100% | An AI can use failed traces to produce a Prompt candidate that passes predefined gates. |
| Three independent repeats | The runs ended in acceptance, external-generation failure, and holdout rollback | Different external Evolver outcomes enter the same audit, stop, and gate flow. |
| Traceable private-system rerun | Framework, target, executed-artifact, and dataset identities were recorded before execution. Candidate one rolled back and candidate two was accepted. | The result is bound to the executed identity while preserving rollback and acceptance decisions from the same run. |
| Single-machine concurrency integrity | 1, 8, and 32 workers each processed 1,000 cases with 1,000 unique results, no hard failure, and recovery from all 10 transient failures | Bounded threaded concurrency did not lose or duplicate cases, and retry outcomes remained auditable. |
| Failure accounting and resume | All five permanent failures were recorded. Resuming from 500 to 1,000 cases produced 1,000 unique results. Two processes wrote 50 separate runs without loss. | Permanent errors are not swallowed. Staged resume and concurrent SQLite writers preserve record integrity. |
| Short soak | 10 seconds, 51 consecutive batches, 5,100 cases, no failure, and one thread both before and after | Results remain complete across repeated runs and worker threads are reclaimed after each batch. |

## Single-machine concurrency baseline

The keyless synthetic stress results were collected on Windows 11 with Python 3.12.13 and are stored in [evidence/concurrency-results.json](evidence/concurrency-results.json). Each setting processed 1,000 cases with ten injected first-call failures. With `retries=1`, every setting produced 1,000 unique results and no hard failure.

| Workers | Throughput | P95 Agent latency | Peak traced memory |
| ---: | ---: | ---: | ---: |
| 1 | 434.29 cases/s | 1.64 ms | 9,975,748 bytes |
| 8 | 530.52 cases/s | 2.21 ms | 10,055,181 bytes |
| 32 | 512.65 cases/s | 2.67 ms | 10,163,531 bytes |

These throughput values are a snapshot from one local run and are not production capacity targets. The synthetic Agent sleeps for only about one millisecond per call, so this profile mostly measures thread scheduling, rule evaluation, and SQLite write overhead. The host reached overhead saturation after eight workers, so instantaneous throughput did not keep rising at 32 workers. This was not a stability failure: every profile produced 1,000 unique results, no hard failure, and complete transient-failure recovery. A bounded in-flight window of twice the worker count kept traced memory for 1,000 cases near 10 MB. The evidence supports shared-state integrity under bounded single-machine concurrency. Capacity for a real HTTP Agent still requires an M4 test against its own latency, rate limits, and SLOs.

The short repeated-run result is stored in [evidence/soak-results.json](evidence/soak-results.json). It completed 51 batches and 5,100 cases in ten seconds with no failure. Thread count was one both before and after, and peak Python traced memory was about 2.48 MB.

## Evaluator-Skill evidence

The keyless script includes `evaluator_skill_evolution`. Its data simulates future human-review history. One simulated misjudgment enters improvement, two reports enter regression, and one historical report hidden from candidate generation enters holdout. These samples use synthetic Gold and have not been reviewed by the user or a domain expert. They are explicitly marked `human_reviewed=false` and `review_status=simulated`. SHA-256 hashes for all three files are stored in [evidence/verified-results.json](evidence/verified-results.json).

The baseline only recognizes an older error pattern and scores 0% on improvement and holdout. The first candidate replaces the old rule with a new rule and breaks previously correct historical judgments, so regression rolls it back. The second candidate preserves the old rule and generalizes a new pattern from improvement. It reaches 100% on all three sets and is accepted inside the workspace.

This is deterministic evidence for the historical-report replay mechanism. It does not establish evaluator quality on real human reviews. Concurrency mechanics do not depend on target semantics, so the project does not duplicate the pressure engine for evaluator Skills. They use the same `EvaluationEngine`, SQLite store, run lock, and bounded workers.

On August 31, 2026, the user reviewed ten general synthetic cases one by one. Two incorrect or partially correct reports entered improvement. Four confirmed-correct reports entered regression. The remaining two confirmed-correct and two partially correct reports were frozen as holdout. `PARTIALLY_CORRECT` remains the original human outcome but maps to `INCORRECT` at the binary release gate so that incomplete evaluations do not pass. The original review appears in [evidence/评测Skill人工审核表.md](evidence/评测Skill人工审核表.md). Machine-readable samples appear in `examples/evaluator_skill_human.*.jsonl`, and their counts and SHA-256 hashes are stored in [evidence/verified-results.json](evidence/verified-results.json).

REVIEW-007 and REVIEW-008 were initially labeled correct and later adjudicated as partially correct. The initial decision, reviewed conclusion, and final binary gate were all preserved. These samples demonstrate that business-provided Gold may itself remain ambiguous for a time. The framework therefore includes an `UNRESOLVED` review state. An unresolved sample cannot enter candidate generation, regression, holdout, or the denominator of a published accuracy result.

The ten samples now drive an independent evaluator-Skill evolution task. Under the latest Gold, the baseline scores 0%, 100%, and 25% on improvement, regression, and holdout. An overcorrection that rejects every passing report breaks regression. A candidate that checks several decisive facts reaches 100%, 100%, and 75% but still misses REVIEW-008. The evaluator-Skill policy requires every holdout case to pass, so both candidates are rolled back and the active version is unchanged. The author wrote these deterministic reference candidates in advance. They establish gate behavior and do not establish generalization to unseen data.

A separate stability experiment froze a manually written evaluator Skill, `Qwen/Qwen3-14B`, temperature 0, and a 128-token output limit, then scored the complete ten-case set over five rounds. All 50 calls succeeded and consumed 11,969 tokens. Every case received the same decision in all five rounds, giving 100% repeat stability. At experiment time, REVIEW-008 still had a human Gold of `CORRECT`, so agreement was 90% in each round. In all five rounds, the model noticed that the report had changed “no data loss occurred” into “no data loss was recorded.” The business reviewer then adjudicated the report as `PARTIALLY_CORRECT`, which maps to `INCORRECT` at the binary gate. Rescoring the unchanged model outputs produced 100% agreement in every round. Raw results are in [evidence/evaluator-skill-stability-results.json](evidence/evaluator-skill-stability-results.json), and the post-adjudication rescore is in [evidence/evaluator-skill-stability-adjudicated.json](evidence/evaluator-skill-stability-adjudicated.json). This experiment measures repeat scoring by a fixed Skill. It does not test candidate generation or generalization to real business data.

The deterministic reference generator supplied the candidates used to verify that human-reviewed data actually controls modification, regression, and holdout gates. It does not show that an AI independently discovered those rules, or that ten synthetic cases represent a real business distribution.

Aggregate results for the private external reference system are available in the [first-run summary](evidence/ai-health-prompt-evolution-summary.json), [three-repeat summary](evidence/ai-health-repeat-results.json), and [traceable-rerun summary](evidence/ai-health-provenance-rerun-summary.json). The repository does not distribute that system's source, Prompt, business cases, adapter, or internal reports.

## Data and experimental boundaries

- The private external reference system uses synthetic cases, not real business data or production bad cases.
- Evaluator-Skill data also represents synthetic human-review history, not an accumulated production review set.
- The private external experiment validates real system integration, not business outcomes, and does not predict success for another system.
- AI candidate generation is stochastic. Structure boundaries, budgets, minimum improvement, regression, and holdout gates handle external Evolver results.
- Multi-file snapshots and a container runner are implemented. Build caching and multi-hour soak remain demand-driven extensions.

## Why the experiments use a 14B model

AI-assisted mechanism experiments intentionally use `Qwen/Qwen3-14B`. The project measures whether candidate changes can be isolated, reevaluated, accepted, or rolled back. It does not benchmark foundation-model capability. Invalid or incorrect output from a smaller model is a normal input handled through structure boundaries, regression protection, and holdout gates.

The 14B results do not represent a model capability ceiling and are not combined with results from larger models on different tasks. A stronger model may produce useful candidates more often, but it does not change hard gates, human Gold, or dataset isolation. A future model-size comparison would use the same task, baseline, frozen data, budget, and repeat count. It would not rewrite the current evidence.

In the evaluator-Skill AI evolution experiment on August 31, 2026, the 14B model correctly diagnosed two incorrect judgments in improvement. Candidate generation then returned the unchanged `MODE=constant-correct` baseline and made no effective modification. Improvement remained 0%, regression remained 100%, and the historical frozen holdout remained 75%. The candidate failed the minimum-improvement gate and was rejected without replacing the baseline. REVIEW-007 and REVIEW-008 were adjudicated after the experiment, bringing the current holdout baseline to 25%. The historical snapshot was preserved. The compact result is in [evidence/evaluator-skill-ai-14b-summary.json](evidence/evaluator-skill-ai-14b-summary.json), and the public audit is in [evidence/evaluator-skill-ai-14b-audit.json](evidence/evaluator-skill-ai-14b-audit.json). This result validates the failure path. It is not evidence that the Skill improved.

## Evidence levels

### Framework mechanics

The keyless deterministic flow and automated tests establish mechanism behavior. Any contributor can reproduce them locally or in CI.

### Private real-system integration

Aggregate results show that the framework used a real interface, version switching, and traces from an external Agent system. That project is not distributed with this repository, so these results are supplementary integration records rather than the public reproducibility basis for framework mechanics. They do not predict outcomes for another business system.

### Business outcomes

Business outcomes remain unproven. They require a real business test set, expert Gold, historical bad cases, or production feedback. The framework cannot generate that evidence for itself.

## Accurate public claim

> When a domain owner supplies test sets, evaluation rules, and permitted change boundaries, this framework provides a controlled, auditable, and reversible loop for automated Agent and Skill evaluation and candidate evolution.

## Claims this evidence does not support

- Every Agent improves automatically.
- An AI can define business truth by itself.
- Self-evolution works without a test set.
- A candidate can go directly to production without gates.
- The current experiments prove real clinical benefit.
