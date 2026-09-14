# S5 — drift adapter

The existing drift mutation set and its blind spots remain authoritative. This adapter
maps its output to the common run vocabulary and exposes detection, latency, silent
staleness, false-drift, and reconciliation-cost metrics. It intentionally does not repair
the known flat-import package-boundary failure; that dependency remains WI037.

S5 is model-independent under `2026-09-14.s5-model-independent.1`: each registered
mutation/condition/repetition is one deterministic validator observation with zero
task turns and no model label. The execution plan therefore contains 135 shared S5
cells, not two copies of the same observation under separate model families.
