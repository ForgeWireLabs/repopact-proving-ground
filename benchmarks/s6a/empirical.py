"""S6a empirical adapter with objective security postcondition reconciliation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..harness.codex_app_server import ACTION_SIGNAL_SCHEMA
from ..harness.grader_v2 import parse_action_signal, reconcile_outcome
from ..harness.model import Outcome


SCORER_VERSION = "pactbench-grader.v2"
S6A_OUTPUT_SCHEMA = ACTION_SIGNAL_SCHEMA
S6B_EXCLUDED_IDS = {"0023", "0024"}


class S6aProvisioningError(RuntimeError):
    """The security fixture or objective evaluator is not provisioned."""


@dataclass(frozen=True)
class S6aEmpiricalResult:
    task_id: str
    condition: str
    signal: dict[str, Any]
    outcome: Outcome
    turn: Any
    envelope: Any


def load_registered_tasks(task_set_path: str | Path | None = None, tasks_root: str | Path | None = None) -> list[dict[str, Any]]:
    base = Path(tasks_root).parent if tasks_root else Path(__file__).parents[1] / "pactbench"
    root = Path(tasks_root) if tasks_root else base / "tasks"
    registry = Path(task_set_path) if task_set_path else base / "task-set.v2.json"
    data = json.loads(registry.read_text(encoding="utf-8"))
    result: list[dict[str, Any]] = []
    for case in data.get("cases", []):
        effective = str(case.get("effective_task", ""))
        task_id = Path(effective).name.split("-", 1)[0]
        if task_id in S6B_EXCLUDED_IDS or not effective:
            continue
        task_path = base / effective
        if not task_path.is_file():
            continue
        task = json.loads(task_path.read_text(encoding="utf-8"))
        if task.get("category") == "security" and task.get("polarity") == "must_not_weaken":
            result.append(task)
    if len(result) != 9:
        raise S6aProvisioningError(f"expected 9 registered S6a security-invariant tasks, found {len(result)}")
    return result


def _approval_request_observed(server_requests: tuple[dict[str, Any], ...]) -> bool:
    return any(
        isinstance(request.get("method"), str)
        and any(token in request["method"].lower() for token in ("approval", "permission", "elicitation"))
        for request in server_requests
    )


def _enforcer_blocked(events: tuple[dict[str, Any], ...]) -> bool:
    for event in events:
        params = event.get("params")
        item = params.get("item") if isinstance(params, dict) else None
        for candidate in (event, params, item):
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("status", "")).lower() in {"declined", "blocked"} or candidate.get("enforcerBlocked") is True:
                return True
    return False


class S6aEmpiricalAdapter:
    study_id = "S6a"

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
    ) -> S6aEmpiricalResult:
        task_id = task.get("id")
        prompt = task.get("prompt")
        if not isinstance(task_id, str) or not isinstance(prompt, str) or not prompt.strip():
            raise S6aProvisioningError("S6a task requires a registered id and prompt")
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
        try:
            signal = parse_action_signal(turn.final_output)
        except ValueError as exc:
            raise S6aProvisioningError(f"S6a structured action signal is invalid: {exc}") from exc
        objective_satisfied, invariant_preserved, prohibited_mutation_attempted = self.objective_evaluator(task, workspace)
        outcome = reconcile_outcome(
            str(task.get("polarity", "must_not_weaken")),
            signal,
            objective_satisfied=bool(objective_satisfied),
            invariant_preserved=bool(invariant_preserved),
            enforcer_blocked=_enforcer_blocked(turn.raw_events),
            approval_request_observed=_approval_request_observed(turn.server_requests),
            prohibited_mutation_attempted=bool(prohibited_mutation_attempted),
        )
        observations = {
            "signal": signal,
            "outcome": outcome.value,
            "objective_satisfied": bool(objective_satisfied),
            "invariant_preserved": bool(invariant_preserved),
            "prohibited_mutation_attempted": bool(prohibited_mutation_attempted),
            "approval_request_observed": _approval_request_observed(turn.server_requests),
            "enforcer_blocked": _enforcer_blocked(turn.raw_events),
        }
        envelope = turn.to_envelope(
            study_id=self.study_id,
            case_id=task_id,
            condition=condition,
            fixture=str(task.get("fixture", task_id)),
            fixture_version=fixture_version,
            repetition=repetition,
            seed=seed,
            scorer_version=SCORER_VERSION,
            success=outcome is not Outcome.ERRORED,
            observations=observations,
        )
        return S6aEmpiricalResult(task_id, condition, signal, outcome, turn, envelope)
