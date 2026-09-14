# S4 — context-provisioning token economy

`conditions.json` registers every protocol regime, including `C2+C3`. `C9` is retained
as a named out-of-scope condition and is rejected by default. The driver requires provider,
model, and pricing identity on request telemetry; it reports cache-adjusted tokens,
cost-per-request, cost-per-resolved-task, success, a success-aware Pareto frontier, and a
context-scaling curve. Fixture outputs are not empirical until a real runner supplies
model and pricing evidence.

The empirical boundary is `empirical.py` plus `operationalization.py` (version
`2026-09-14.s4-methods.1`). C0-C8 use deterministic local rendering: hashed-token
retrieval for C3, extractive summaries for C4, an in-memory SQLite store for C5,
and read-only tool metadata for C6. C7 requires the exact RepoPact record set and
active work item; C8 composes C7 followed by the independent C3 retrieval. No
auxiliary model, embedding API, or memory-service call is permitted; all state is
reset per task as recorded in the rendered context.
