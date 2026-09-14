# Benchmark harness

Model-agnostic harness for RepoPact's comparative benchmark protocol. It loads frozen
PactBench tasks, runs matched arms, grades S1/S6a outcomes, and provides the study-neutral
`RunEnvelope` used by the S2-S6 drivers. Study-specific observations remain outside the
envelope so recovery, coordination, economy, drift, and injection results are not forced
into the PactBench confusion matrix.

## Run it

```bash
# Illustrative pipeline check against the pre-registered PactBench tasks:
python benchmarks/harness/run.py

# Self-test (asserts the pipeline produces a sane matrix; exits non-zero on failure):
python benchmarks/harness/run.py --selftest

# Write a report file, choose arms:
python benchmarks/harness/run.py --arms baseline,repopact --out report.md
```

## What is real vs. operator-gated

| Piece | Status |
|---|---|
| Task loader, arm runner, grader, confusion-matrix + metrics, token instrumentation | **implemented**, self-tested |
| `MockRunner` | implemented — deterministic test double; **numbers are illustrative, not findings** |
| `RealRunner` (drive a live agent over a fixture, read post-conditions) | **operator-gated** — needs a model/agent + API keys |
| `RealRunnerV2` + `codex_real_runner_v2.py` | **operator-gated** — public Codex app-server, corrected PactBench materialization, request-level ledger |

The MockRunner exists so the plumbing is testable without a model. Real results require the
RealRunner across at least two model families (work item 022, AC-3); until then no row here
is a finding.

## The runner interface

A runner implements `run(task, arm, study_spec) -> AgentAction`. `RealRunner` sends a
versioned request (`repopact.real-runner.v1`) to the command named by `REPOPACT_AGENT_CMD`.
The wrapper must return structured action flags, study observations, model/provider/version
identity, provenance, raw capture reference, and complete per-request plus aggregate
telemetry. Missing telemetry is an invalid runner response; it is never converted into
zeroes. The grader (`graders.py`) turns an action into an `Outcome`; runners never assign
outcomes themselves.

The common envelope is `repopact.experiment-run.v1`. It records study/case/condition,
fixture and task-set versions, repetition/seed, model identity, policy/scorer versions,
completion/failure state, request telemetry, aggregate telemetry, observations, exact
command, raw capture reference, provenance, and an explicit illustrative classification.

The corrected v2 contract (`repopact.real-runner.v2` / `repopact.experiment-run.v2`) is
additive and leaves v1 reading intact. A v2 request is one completed provider inference
response, accounted from the public `thread/tokenUsage/updated` notification's advancing
`last`/`total` ledger. It preserves provider cache reads, cache writes, reasoning output,
and deterministic context/task attribution; aggregate telemetry must equal the exact sum
of request records. The structured `pactbench.action-signal.v1` is reconciled with
repository postconditions, so no diff alone is treated as a block or escalation.

## Files

| File | Role |
|---|---|
| `model.py` | Backward-compatible PactBench types plus expanded telemetry |
| `execution.py` | Study-neutral run envelope and strict telemetry validation |
| `run.schema.json` | Machine-readable envelope shape/version |
| `runners.py` | Versioned `MockRunner`, `RealRunner` (gated), `get_runner` |
| `codex_app_server.py` | Public app-server stdio client and advancing token ledger feed |
| `empirical.py` | Shared strict-schema empirical turn, telemetry, capture, and provenance boundary |
| `empirical_workspace.py` | Windows-safe disposable live workspace allocator, ACL profile, direct-sandbox preflight, and security fingerprint |
| `ac3_execution_manifest.py` | Generates the pre-inference AC-3 manifest; it never starts a cell |
| `admission_probe.py` | One-shot disposable family admission probe, outside the registered matrix |
| `codex_real_runner_v2.py` | Corrected three-case smoke adapter with preflight and captures |
| `codex_usage.py` | Pinned tokenizer and strict v2 usage accounting |
| `registry.py` | Deterministic pre-registration ordering and file digests |
| `capture.py` | Stable capture layout, classification, and secret checks |
| `graders.py` | action → outcome classification (polarity-aware) |
| `report.py` | confusion matrix, metrics, token summary, markdown render |
| `run.py` | CLI: load → run → grade → report; `--selftest` |

## Coverage

Coverage includes the deterministic driver/scorer boundaries for S2 recovery, S3
coordination, S4 context economy, the existing S5 drift adapter, and S6b injection
resistance. S2's external beds are pinned by immutable revision and selector manifests;
materialization is explicit and does not vendor third-party task material. These drivers
are executable plumbing, not agent-behaviour findings. Live comparative execution remains
operator-gated on a provisioned runner and model credentials.

The AC-3 empirical adapters are intentionally narrow: `s2/empirical.py` requires a
verified pinned materialization plus a study-built functional/evaluation bed;
`s3/empirical.py` creates two concurrent isolated worker turns; `s4/empirical.py`
uses the frozen local renderers in `s4/operationalization.py`; and `s6a`/`s6b`
require objective filesystem/postcondition evaluators. The existing deterministic
drivers and S5 fixture self-tests remain illustrative/non-empirical.

On Windows, live empirical workspaces are allocated below `REPOPACT_BENCH_WORK_ROOT`
when set, or the repository's `.repopact-bench-workspaces` directory by default. The
allocator avoids Python temp-directory defaults, applies a disposable protected DACL with
explicit host and Codex sandbox-user SIDs, validates path/reparse safety, and proves
host read/write plus direct sandbox create/modify/read-back before an app-server turn.
The normalized ACL digest and preflight result are recorded in empirical provenance.
