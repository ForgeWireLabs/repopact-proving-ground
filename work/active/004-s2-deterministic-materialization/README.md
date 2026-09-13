# 004 — Make S2 task beds reproducibly materializable

> **Status**: Active
> **Owner**: governance-owner

## Intent

Close the final deterministic/manual gap in WI022 without running a model or
committing third-party benchmark data. The S2 materializer will acquire the
registered SWE-bench Verified Parquet asset and SWE-EVO Arrow payload from their
immutable sources, validate the selected records, separate model and grader
projections, record reproducible manifests, and support offline verification.

## Acceptance criteria

- [ ] **S2-001** Both registered beds acquire immutable assets and reject moving
  revisions or digest mismatches.
- [ ] **S2-002** Registered selectors are found exactly once, validated against
  their real schemas, and projected without gold-solution leakage.
- [ ] **S2-003** Manifests have stable identity fingerprints and offline
  verification detects tampering.
- [ ] **S2-004** All six source/base commits resolve in bounded preflight; no
  model or third-party payload is committed.
