# 001 — Temperature and length conversion

> **Status**: ✅ Complete
> **Owners**: app-owner (lead).
> **Depends on**: none.

## Intent

Give the proving ground a real, testable behavior to govern: convert temperatures
across C/F/K and lengths across m/km/ft/mi. In scope: the `unitconv` conversion
functions and their tests. Out of scope: a persistent UI, additional unit families.

## Decisions

Conversions pivot through a single canonical unit (Celsius for temperature, metres
for length) so each unit needs only one pair of factors. This keeps the table
small and the round-trip property easy to test.

## Scope

- `unitconv.py` — `convert_temperature`, `convert_length`, and the CLI.
- `tests/test_unitconv.py` — 8 unit tests including round-trips and error handling.

## Closeout

Both acceptance criteria are satisfied by evidence run
`20260615-202635-unitconv-tests` (8 tests pass). On transition the directory moves
to `work/completed/` and the dashboard is regenerated.
