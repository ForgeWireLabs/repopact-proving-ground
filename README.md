# RepoPact Proving Ground

> **RepoPact defines the pact. RepoPact Proving Ground tests whether the pact holds under
> agent pressure.**

This repository is the adversarial lab for [RepoPact](https://github.com/ForgeWireLabs/repopact).
It is a small, throwaway-but-genuinely-working project (`unitconv.py`, a unit-converter CLI)
that **adopts RepoPact from PyPI** and is then driven across every RepoPact primitive —
including cases designed specifically to break the architecture. The point is evidence, not a
demo: if a guarantee can be silently weakened, this is where it shows up.

## What this repo is

- **A real subject under test.** `unitconv.py` is actual working code with real work items,
  evidence, and decisions — not a mock. Its job is to be governed, and to be attacked.
- **Itself a governed RepoPact repo.** Its own contracts (`AGENTS.md`, `governance/`,
  `work/`, `evidence/`, `audits/`) are validated by RepoPact, so the lab eats its own dog
  food while testing the kernel.
- **Run against the *packaged* product.** It consumes RepoPact from PyPI (pinned in
  [`requirements-repopact.txt`](requirements-repopact.txt)), not a source checkout — so it
  tests exactly what an adopter receives, on a clean install.

## Why it exists

Capable models still produce unreliable systems. RepoPact's claim is that a thin layer of
machine-checked records makes the *binding* part of a project enforceable. That claim is only
worth anything if it survives adversarial pressure, so the Proving Ground:

- adopts the published package and reaches a *valid* governed repository with the documented
  commands;
- drives the happy path (`init → new → implement → evidence → validate → transition`); and
- deliberately attempts each falsification case — fabricated evidence, silent invariant
  weakening, status/directory mismatch, frozen-surface edits — and records whether the
  architecture catches it.

Defects found here are fed back into RepoPact using RepoPact's own machinery. The early
adversarial findings (e.g. **F-001** and **F-002**) were surfaced in this proving ground,
fixed upstream, and re-verified from a rebuilt package.

## Run it

```bash
pip install -r requirements-repopact.txt   # installs RepoPact from PyPI
repopact validate                          # validate this repo's own contracts
python -m unittest discover -s tests       # exercise unitconv
```

## Where the evidence lives

The pre-registered experiment protocol, the findings register (F-001…F-013), the raw
captures, the formal model, and the paper are kept in RepoPact's public `research/` lab
notebook:

- [protocol.md](https://github.com/ForgeWireLabs/repopact/blob/main/research/protocol.md) —
  hypotheses + falsification criteria, set before the runs.
- [findings.md](https://github.com/ForgeWireLabs/repopact/blob/main/research/findings.md) —
  what cracked, what held.
- [formal-model.md](https://github.com/ForgeWireLabs/repopact/blob/main/research/formal-model.md)
  and [paper.md](https://github.com/ForgeWireLabs/repopact/blob/main/research/paper.md).

**PactBench**, the runnable benchmark suite (pre-registered tasks + harnesses measuring
whether enforcement reduces silent guarantee drift), currently lives in the RepoPact repo
under [`benchmarks/`](https://github.com/ForgeWireLabs/repopact/tree/main/benchmarks).

## Relationship to RepoPact and ForgeWire Labs

RepoPact (the standard) defines the contract language, the reference validator, and the
benchmark protocol. This Proving Ground exercises that standard under adversarial agent
pressure and produces the reproducible evidence behind RepoPact's claims. Both are part of
[ForgeWire Labs](https://github.com/ForgeWireLabs) — inspectable agentic infrastructure.
