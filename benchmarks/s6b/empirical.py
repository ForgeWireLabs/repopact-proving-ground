"""S6b empirical adapter separating injection-following from structural detection."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .driver import InjectionObservation, SCORER_VERSION, score


S6B_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["task_id", "completed", "evidence"],
    "properties": {
        "task_id": {"type": "string", "enum": ["0023", "0024"]},
        "completed": {"type": "boolean"},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
}


class S6bProvisioningError(RuntimeError):
    """The poisoned-context fixture lacks objective security grading."""


@dataclass(frozen=True)
class S6bEmpiricalResult:
    observation: InjectionObservation
    scored: dict[str, Any]
    turn: Any
    envelope: Any


class S6bEmpiricalAdapter:
    study_id = "S6b"

    def __init__(self, executor: Any, *, objective_evaluator: Callable[[dict[str, Any], Any], tuple[bool, bool, bool]]) -> None:
        self.executor = executor
        self.objective_evaluator = objective_evaluator

    def run_case(
        self,
        task: dict[str, Any],
        condition: str,
        *,
        workspace: Any,
        fixture_version: str,
        workspace_identity: dict[str, Any],
        capture_name: str,
        repetition: int = 0,
        seed: int = 0,
    ) -> S6bEmpiricalResult:
        task_id = task.get("id")
        prompt = task.get("prompt")
        if task_id not in {"0023", "0024"} or not isinstance(prompt, str) or not prompt.strip():
            raise S6bProvisioningError("S6b requires one of the two registered task prompts")
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
            workspace_identity=workspace_identity,
            auxiliary_calls=(),
        )
        # The model's structured response is retained as evidence, but neither
        # ``completed`` nor any self-report is used as a security outcome.
        injection_followed, structurally_detected, completed = self.objective_evaluator(task, workspace)
        observation = InjectionObservation(task_id, condition, bool(injection_followed), bool(structurally_detected), bool(completed))
        scored = score([observation])
        envelope = turn.to_envelope(
            study_id=self.study_id,
            case_id=task_id,
            condition=condition,
            fixture=str(task.get("fixture", task_id)),
            fixture_version=fixture_version,
            repetition=repetition,
            seed=seed,
            scorer_version=SCORER_VERSION,
            success=bool(completed),
            observations={"objective": {"injection_followed": observation.injection_followed, "structurally_detected": observation.structurally_detected}, "score": scored, "model_report": turn.final_output},
        )
        return S6bEmpiricalResult(observation, scored, turn, envelope)
