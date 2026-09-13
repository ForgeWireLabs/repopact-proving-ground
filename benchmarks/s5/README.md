# S5 — drift adapter

The existing drift mutation set and its blind spots remain authoritative. This adapter
maps its output to the common run vocabulary and exposes detection, latency, silent
staleness, false-drift, and reconciliation-cost metrics. It intentionally does not repair
the known flat-import package-boundary failure; that dependency remains WI037.
