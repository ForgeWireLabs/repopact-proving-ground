# S2 — cross-session recovery and efficiency

`task-set.json` freezes the dataset locators, immutable revisions, and task selectors
before any run. The task material is intentionally not vendored; `materialize.py` is the
reproducible acquisition boundary and refuses moving revisions. SWE-bench Verified uses
the pinned Hugging Face revision; SWE-EVO uses its pinned upstream repository revision.

The driver records resolution, regressions/invariant violations, tokens to completion,
human interventions, and the three-part recovery rubric: goal, prior decisions, and
remaining work. Its output is illustrative until a provisioned live runner supplies
real model identity, telemetry, and captures.

Install the acquisition-only parser, then materialize either bed:

```text
python -m pip install -r requirements-s2.txt
python benchmarks/s2/materialize.py --bed swe-bench-verified --out .s2-materialized
python benchmarks/s2/materialize.py --bed swe-evo --out .s2-materialized
```

The materializer acquires only the registered immutable source, verifies bytes
before atomic placement, validates the selected records and base commits, and
writes separate model-facing and evaluation projections. The model-facing
projection excludes patches and test patches. Reproducibility can be checked
without network access:

```text
python benchmarks/s2/materialize.py --bed swe-bench-verified --out .s2-materialized --verify
python benchmarks/s2/materialize.py --bed swe-evo --out .s2-materialized --verify
```

The output directory is intentionally ignored by Git. No model, inference
runner, or third-party benchmark payload is part of this repository.

`empirical.py` is a separate adapter. It accepts only a verified pinned
materialization and a study-built functional/evaluation bed, creates matched
baseline/RepoPact seeds, and grades objective postconditions. It does not convert
the recovery metric `tokens_to_completion` into telemetry.
