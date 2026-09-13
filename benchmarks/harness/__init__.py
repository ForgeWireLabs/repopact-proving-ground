"""RepoPact benchmark harness.

A model-agnostic harness for the comparative studies. It loads pre-registered tasks, runs
matched arms, grades PactBench outcomes, and supplies a study-neutral run envelope for the
S2-S6 drivers.

The live-model runner is operator-gated (compute + API keys). A deterministic ``MockRunner``
ships so the harness plumbing is testable and self-checkable without a model; its numbers
are illustrative plumbing checks, **not** findings.
"""
