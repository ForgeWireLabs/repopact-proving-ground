# S2 — cross-session recovery and efficiency

`task-set.json` freezes the dataset locators, immutable revisions, and task selectors
before any run. The task material is intentionally not vendored; `materialize.py` is the
reproducible acquisition boundary and refuses moving revisions. SWE-bench Verified uses
the pinned Hugging Face revision; SWE-EVO uses its pinned upstream repository revision.

The driver records resolution, regressions/invariant violations, tokens to completion,
human interventions, and the three-part recovery rubric: goal, prior decisions, and
remaining work. Its output is illustrative until a provisioned live runner supplies
real model identity, telemetry, and captures.
