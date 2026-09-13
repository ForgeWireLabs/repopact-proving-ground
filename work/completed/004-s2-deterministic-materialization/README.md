# 004 — Make S2 task beds reproducibly materializable

> **Status**: Complete
> **Owner**: governance-owner

## Intent

Close the final deterministic/manual gap in WI022 without running a model or
committing third-party benchmark data. The S2 materializer will acquire the
registered SWE-bench Verified Parquet asset and SWE-EVO Arrow payload from their
immutable sources, validate the selected records, separate model and grader
projections, record reproducible manifests, and support offline verification.

## Acceptance criteria

- [x] **S2-001** Both registered beds acquire immutable assets and reject moving
  revisions or digest mismatches.
- [x] **S2-002** Registered selectors are found exactly once, validated against
  their real schemas, and projected without gold-solution leakage.
- [x] **S2-003** Manifests have stable identity fingerprints and offline
  verification detects tampering.
- [x] **S2-004** All six source/base commits resolve in bounded preflight; no
  model or third-party payload is committed.

## Evidence

`20260913-004-s2-deterministic-materialization` records the exact pinned
source assets, selected IDs, per-record digests, base-commit preflight, stable
fingerprints, offline verification, and the absence of tracked materialization
payloads. No model or empirical benchmark run was performed.
