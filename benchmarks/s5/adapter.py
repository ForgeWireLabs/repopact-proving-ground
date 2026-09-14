"""Adapt the existing S5 drift harness into the common run envelope."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import hashlib
from statistics import mean
from typing import Any

try:
    from ..harness.execution import ModelIdentity, RunEnvelope
    from ..harness.model import TokenUsage
except ImportError:  # pragma: no cover
    from harness.execution import ModelIdentity, RunEnvelope  # type: ignore
    from harness.model import TokenUsage  # type: ignore


SCORER_VERSION = "s5-drift-adapter.v1"
EXECUTION_METHOD_VERSION = "2026-09-14.s5-model-independent.1"
MODEL_INDEPENDENT = True
REGISTERED_CONDITIONS = ("C2", "C2+C3", "C7")


def condition_implementation_fingerprint(condition: str) -> str:
    if condition not in REGISTERED_CONDITIONS:
        raise ValueError(f"S5 condition is not registered: {condition}")
    return hashlib.sha256(f"{EXECUTION_METHOD_VERSION}|{condition}|deterministic-validator".encode()).hexdigest()


@dataclass(frozen=True)
class DriftObservation:
    mutation_id: str
    condition: str
    detected: bool
    latency: int | str
    silent_staleness: bool
    false_drift: bool
    reconciliation_cost: int
    blind_spot: bool = False


@dataclass(frozen=True)
class S5ExecutionCell:
    """One deterministic drift observation; it has no model arm."""

    mutation_id: str
    condition: str
    repetition: int
    seed: int
    model: None = None
    expected_task_turns: int = 0
    auxiliary_call_class: str = "deterministic-validator"


def build_execution_plan(
    mutation_ids: Iterable[str], *, repetitions: int = 3,
) -> tuple[S5ExecutionCell, ...]:
    """Build the registered S5 plan without pseudo-replicating model labels."""
    if repetitions < 1:
        raise ValueError("S5 repetitions must be positive")
    cells: list[S5ExecutionCell] = []
    for mutation_id in sorted(set(mutation_ids)):
        if not mutation_id:
            raise ValueError("S5 mutation ids must be non-empty")
        for condition in REGISTERED_CONDITIONS:
            for repetition in range(repetitions):
                seed = int.from_bytes(
                    hashlib.sha256(f"S5|{EXECUTION_METHOD_VERSION}|{mutation_id}|{condition}|{repetition}".encode()).digest()[:8],
                    "big",
                )
                cells.append(S5ExecutionCell(mutation_id, condition, repetition, seed))
    validate_execution_plan(cells)
    return tuple(cells)


def validate_execution_plan(cells: Iterable[S5ExecutionCell]) -> None:
    seen: set[tuple[str, str, int]] = set()
    for cell in cells:
        if not isinstance(cell, S5ExecutionCell):
            raise TypeError("S5 plan entries must be S5ExecutionCell")
        if cell.model is not None or cell.expected_task_turns != 0:
            raise ValueError("S5 deterministic cells cannot carry a model or task turn")
        key = (cell.mutation_id, cell.condition, cell.repetition)
        if key in seen:
            raise ValueError(f"duplicate S5 deterministic cell: {key}")
        seen.add(key)


def adapt_result(raw: dict[str, Any], *, condition: str = "repopact", fixture_version: str = "drift-mutations.v1") -> DriftObservation:
    """Map legacy S5 keys without changing its registered mutation predictions."""
    if not isinstance(raw.get("id"), str):
        raise ValueError("S5 result requires mutation id")
    detected = bool(raw.get(f"{condition}_detected", raw.get("repopact_detected", False)))
    latency = raw.get(f"{condition}_latency", raw.get("repopact_latency", "inf"))
    return DriftObservation(
        mutation_id=raw["id"], condition=condition, detected=detected,
        latency=latency, silent_staleness=not detected,
        false_drift=bool(raw.get("false_drift", False)),
        reconciliation_cost=int(raw.get("reconciliation_cost", 0)),
        blind_spot=bool(raw.get("blind_spot", False)),
    )


def summarize(observations: Iterable[DriftObservation]) -> dict[str, Any]:
    rows = list(observations)
    if not rows:
        return {"n": 0, "detection_rate": 0.0, "silent_staleness_rate": 0.0, "false_drift_rate": 0.0, "mean_reconciliation_cost": 0.0}
    finite_latency = [row.latency for row in rows if isinstance(row.latency, (int, float))]
    return {
        "n": len(rows),
        "detection_rate": round(sum(row.detected for row in rows) / len(rows), 3),
        "time_or_edits_to_detection": round(mean(finite_latency), 3) if finite_latency else "inf",
        "silent_staleness_rate": round(sum(row.silent_staleness for row in rows) / len(rows), 3),
        "false_drift_rate": round(sum(row.false_drift for row in rows) / len(rows), 3),
        "reconciliation_cost": sum(row.reconciliation_cost for row in rows),
        "blind_spots": [row.mutation_id for row in rows if row.blind_spot],
        "scorer_version": SCORER_VERSION,
    }


def to_envelope(observation: DriftObservation, *, repetition: int = 0, seed: int = 0, fixture_version: str = "drift-mutations.v1") -> RunEnvelope:
    tokens = TokenUsage(requests=0)
    return RunEnvelope(
        schema_version="repopact.experiment-run.v1", study_id="S5",
        case_id=observation.mutation_id, condition=observation.condition,
        fixture="drift/mutations.json", fixture_version=fixture_version,
        repetition=repetition, seed=seed,
        model=ModelIdentity("deterministic-validator", "local", "fixture-selftest"),
        temperature_policy="deterministic", scorer_version=SCORER_VERSION,
        started_at=None, ended_at=None, elapsed_ms=None, completed=True, success=True,
        failure_class=None, per_request=[], aggregate=tokens,
        observations={
            "detected": observation.detected, "latency": observation.latency,
            "silent_staleness": observation.silent_staleness,
            "false_drift": observation.false_drift,
            "reconciliation_cost": observation.reconciliation_cost,
            "blind_spot": observation.blind_spot,
        },
        provenance={"classification": "illustrative", "source": "legacy-s5-drift-harness"},
        illustrative=True,
        notes="S5 adapter preserves known blind spots; output is not empirical.",
    )
