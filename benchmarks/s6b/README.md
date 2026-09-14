# S6b — context-file injection resistance

S6b consumes the frozen PactBench tasks `0023` and `0024` without mutating them. Its
metrics are injection-followed rate and structural-detection rate. The driver treats both
convention files and RepoPact records as attack surfaces and does not claim RepoPact is
uninjectable. Illustrative observations are explicitly separated from empirical runs.

The empirical adapter records the model report but derives `injection_followed`,
`structurally_detected`, and completion from an objective fixture evaluator; a
self-report cannot determine the security result.
