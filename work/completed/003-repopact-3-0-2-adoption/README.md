# 003 — Migrate RepoPact package boundary to 3.0.2

> **Status**: Complete
> **Owner**: governance-owner

## Intent

Move the Proving Ground from the obsolete flat-module boundary to the exact
`repopact==3.0.2` package and supported CLI entry point. Preserve the S5 drift
harness's deterministic behavior while ensuring it exercises the packaged
adopter contract rather than a RepoPact source checkout.

## Acceptance criteria

- [x] **RPU-001** Pin `repopact==3.0.2` and align the README and dependency guidance.
- [x] **RPU-002** Generate the dashboard and pass packaged RepoPact validation.
- [x] **RPU-003** Pass the 22 unit tests, 24-task PactBench selftest, and S5 drift selftest.

## Evidence

`20260913-003-repopact-3-0-2-adoption` records the exact dependency install,
dashboard and validation checks, unit suite, PactBench selftest, and S5 drift
selftest. The evidence is local and concrete; it makes no hosted-CI or
cross-platform claim.
