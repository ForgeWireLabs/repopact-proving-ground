"""S2 task resolution, recovery rubric, and isolated driver boundary."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from ..harness.execution import ModelIdentity, RunEnvelope
    from ..harness.model import TokenUsage
    from ..harness.registry import RegisteredSet, load_registered_set
except ImportError:  # pragma: no cover
    from harness.execution import ModelIdentity, RunEnvelope  # type: ignore
    from harness.model import TokenUsage  # type: ignore
    from harness.registry import RegisteredSet, load_registered_set  # type: ignore


SCORER_VERSION = "s2-recovery-rubric.v1"


@dataclass(frozen=True)
class RecoveryObservation:
    task_id: str
    dataset: str
    resolved: bool
    regression: bool
    invariant_violation: bool
    tokens_to_completion: int
    human_interventions: int
    goal_recovered: bool
    prior_decisions_recovered: bool
    remaining_work_recovered: bool
    fix_rate: float | None = None

    @property
    def state_recovery_score(self) -> float:
        return round(sum((self.goal_recovered, self.prior_decisions_recovered, self.remaining_work_recovered)) / 3, 3)


def load_task_set(path: str | Path | None = None) -> RegisteredSet:
    source = Path(path) if path else Path(__file__).with_name("task-set.json")
    registered = load_registered_set(source, study_id="S2", records_key="task_sets")
    for record in registered.records:
        if not isinstance(record.get("revision"), str) or len(record["revision"]) < 12:
            raise ValueError(f"S2 task set {record.get('id')} needs an immutable revision")
        if not isinstance(record.get("task_ids"), list) or not record["task_ids"]:
            raise ValueError(f"S2 task set {record.get('id')} needs task_ids")
        if record["task_ids"] != sorted(record["task_ids"]):
            raise ValueError(f"S2 task set {record.get('id')} task_ids must be sorted")
    return registered


def score_recovery(observation: RecoveryObservation) -> dict[str, Any]:
    """Apply the fixed S2 rubric; recovery is not replaced by S8 governance continuity."""
    if observation.tokens_to_completion < 0 or observation.human_interventions < 0:
        raise ValueError("S2 effort metrics must be non-negative")
    if not 0 <= observation.state_recovery_score <= 1:
        raise ValueError("state recovery score must be in [0, 1]")
    return {
        "task_id": observation.task_id,
        "resolved": observation.resolved,
        "regression": observation.regression,
        "invariant_violation": observation.invariant_violation,
        "tokens_to_completion": observation.tokens_to_completion,
        "human_interventions": observation.human_interventions,
        "state_recovery_score": observation.state_recovery_score,
        "fix_rate": observation.fix_rate,
        "scorer_version": SCORER_VERSION,
    }


class S2Driver:
    """Execute S2 through a supplied runner; no provider is embedded here."""

    study_id = "S2"

    def __init__(self, task_set: RegisteredSet | None = None) -> None:
        self.task_set = task_set or load_task_set()

    def run_case(self, case: dict[str, Any], condition: str, runner: Any, *, repetition: int = 0, seed: int = 0) -> RunEnvelope:
        result = runner.run(case, condition, study_spec={"study_id": self.study_id, "scorer_version": SCORER_VERSION})
        if not isinstance(result, RecoveryObservation):
            raise TypeError("S2 runner must return RecoveryObservation")
        observations = score_recovery(result)
        tokens = TokenUsage(
            input_tokens=result.tokens_to_completion, task_tokens=result.tokens_to_completion,
            cache_adjusted_input_tokens=result.tokens_to_completion, requests=1,
        )
        return RunEnvelope(
            schema_version="repopact.experiment-run.v1", study_id=self.study_id,
            case_id=result.task_id, condition=condition, fixture=case["id"],
            fixture_version=self.task_set.version, repetition=repetition, seed=seed,
            model=getattr(runner, "model", None) or ModelIdentity("unknown", "unknown", "unknown"),
            temperature_policy="runner-defined", scorer_version=SCORER_VERSION,
            started_at=None, ended_at=None, elapsed_ms=None, completed=True, success=result.resolved,
            failure_class=None, per_request=[tokens], aggregate=tokens,
            observations=observations, provenance={"classification": "illustrative"},
            illustrative=True, notes="S2 driver output is illustrative until a live runner is provisioned.",
        )
