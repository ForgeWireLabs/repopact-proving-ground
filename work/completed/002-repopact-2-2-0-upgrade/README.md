# 002 — Upgrade RepoPact to 2.2.0

> **Status**: Complete
> **Owner**: governance-owner

## Intent

Move the Proving Ground from an interim upstream commit to exact public release
`repopact==2.2.0`, then exercise the packaged dashboard-integrity behavior.

## Acceptance criteria

- [x] **RPU-001** Pin the exact public PyPI release and align the README.
- [x] **RPU-002** Regenerate the dashboard and pass RepoPact validation.
- [x] **RPU-003** Pass the Proving Ground unit and harness selftests.
- [x] **RPU-004** Record evidence, close the item, and publish the upgrade.

## Evidence

`20260718-002-repopact-2-2-0-upgrade` records the exact package install,
canonical validation, 8 unit tests, 24-task PactBench selftest, and drift selftest.
