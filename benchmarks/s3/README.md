# S3 — multi-agent coordination

The pre-registered fixture tasks define dependent worker scopes. `S3Driver` runs two
workers concurrently and gives each a disposable fixture copy (or a caller-supplied
equivalent isolation layer). `score_coordination` reports clobbering/conflicting edits,
duplicated logical work, scope collisions, and joint success. A fake two-worker runner is
appropriate for orchestration tests only; its output is not empirical evidence.
