"""S4 empirical adapter: render a frozen context regime, then grade postconditions."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .driver import ContextObservation, SCORER_VERSION, aggregate_observation
from .operationalization import RenderedContext, render_condition


S4_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["completed", "evidence"],
    "properties": {
        "completed": {"type": "boolean"},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
}


class S4ProvisioningError(RuntimeError):
    """The context condition or objective postcondition is not provisioned."""


@dataclass(frozen=True)
class S4EmpiricalResult:
    task_id: str
    condition: str
    rendered: RenderedContext
    observation: ContextObservation
    aggregate: dict[str, Any]
    turn: Any
    envelope: Any


class S4EmpiricalAdapter:
    study_id = "S4"

    def __init__(
        self,
        executor: Any,
        *,
        objective_evaluator: Callable[[dict[str, Any], Path, Any], tuple[bool, int]],
    ) -> None:
        self.executor = executor
        self.objective_evaluator = objective_evaluator

    def run_case(
        self,
        task: dict[str, Any],
        condition: str,
        *,
        source_root: str | Path,
        workspace: str | Path,
        repopact_root: str | Path | None = None,
        repetition: int = 0,
        seed: int = 0,
        fixture_version: str,
        workspace_identity: dict[str, Any],
        capture_name: str,
    ) -> S4EmpiricalResult:
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise S4ProvisioningError("S4 task requires an id")
        rendered = render_condition(condition, task, source_root, repopact_root=repopact_root)
        prompt = (
            rendered.payload
            + "\n\nComplete the registered task in the workspace. Return JSON with completed and evidence."
        )
        work = Path(workspace)
        if not work.is_dir():
            raise S4ProvisioningError("S4 requires a functional workspace")
        turn = self.executor.run(
            prompt,
            capture_name=capture_name,
            study_id=self.study_id,
            case_id=task_id,
            condition=condition,
            fixture=str(task.get("fixture", task_id)),
            fixture_version=fixture_version,
            repetition=repetition,
            seed=seed,
            workspace_identity={**workspace_identity, "context_fingerprint": rendered.fingerprint},
            auxiliary_calls=rendered.auxiliary_calls,
        )
        success, accumulated_state = self.objective_evaluator(task, work, turn.final_output)
        observation = ContextObservation(
            task_id=task_id,
            condition=condition,
            requests=turn.per_request,
            success=bool(success),
            accumulated_project_state=int(accumulated_state),
        )
        observation.validate()
        aggregate = aggregate_observation(observation)
        envelope = turn.to_envelope(
            study_id=self.study_id,
            case_id=task_id,
            condition=condition,
            fixture=str(task.get("fixture", task_id)),
            fixture_version=fixture_version,
            repetition=repetition,
            seed=seed,
            scorer_version=SCORER_VERSION,
            success=observation.success,
            observations={**aggregate, "context_fingerprint": rendered.fingerprint, "dependencies": rendered.dependencies},
        )
        return S4EmpiricalResult(task_id, condition, rendered, observation, aggregate, turn, envelope)
