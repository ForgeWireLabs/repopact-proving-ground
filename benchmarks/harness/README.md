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

## Files

| File | Role |
|---|---|
| `model.py` | Backward-compatible PactBench types plus expanded telemetry |
| `execution.py` | Study-neutral run envelope and strict telemetry validation |
| `run.schema.json` | Machine-readable envelope shape/version |
| `runners.py` | Versioned `MockRunner`, `RealRunner` (gated), `get_runner` |
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
