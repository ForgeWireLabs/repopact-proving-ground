# S3 — multi-agent coordination

The pre-registered fixture tasks define dependent worker scopes. `S3Driver` runs two
workers concurrently and gives each a disposable fixture copy (or a caller-supplied
equivalent isolation layer). `score_coordination` reports clobbering/conflicting edits,
duplicated logical work, scope collisions, and joint success. A fake two-worker runner is
appropriate for orchestration tests only; its output is not empirical evidence.

`empirical.py` preserves that design for live execution: one logical cell creates
two independent app-server threads in matched isolated workspaces, writes separate
captures, and derives edit events from filesystem/git state before calling the
existing scorer. The ledger records two live worker turns per logical cell.
