"""S3 empirical two-worker adapter with isolated workspaces and captures."""
from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .driver import (
    SCORER_VERSION,
    CoordinationEvent,
    CoordinationScore,
    WorkerResult,
    isolated_worker_worktrees,
    score_coordination,
)


S3_WORKER_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["worker_id", "completed", "evidence"],
    "properties": {
        "worker_id": {"type": "string", "enum": ["worker-a", "worker-b"]},
        "completed": {"type": "boolean"},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "logical_task": {"type": "string"},
    },
}


class S3ProvisioningError(RuntimeError):
    """The paired worker experiment lacks an objective execution component."""


@dataclass(frozen=True)
class S3EmpiricalResult:
    task_id: str
    condition: str
    score: CoordinationScore
    workers: tuple[WorkerResult, ...]
    turns: tuple[Any, ...]
    envelopes: tuple[Any, ...]


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if not path.is_file() or ".git" in path.relative_to(root).parts:
            continue
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _git_status(root: Path) -> list[str]:
    if not (root / ".git").exists():
        return []
    result = subprocess.run(["git", "-C", str(root), "status", "--short"], capture_output=True, text=True, check=False)
    return [line for line in result.stdout.splitlines() if line]


class S3EmpiricalAdapter:
    study_id = "S3"

    def __init__(
        self,
        executor_factory: Callable[[str, Path], Any],
        *,
        objective_evaluator: Callable[[dict[str, Any], str, Path, Any], bool],
    ) -> None:
        self.executor_factory = executor_factory
        self.objective_evaluator = objective_evaluator

    def run_case(
        self,
        task: dict[str, Any],
        condition: str,
        *,
        source: str | Path,
        repetition: int = 0,
        seed: int = 0,
        fixture_version: str,
        workspace_identity: Callable[[str, Path], dict[str, Any]],
        capture_name: Callable[[str], str],
    ) -> S3EmpiricalResult:
        if set(task.get("worker_scopes", {})) != {"worker-a", "worker-b"}:
            raise S3ProvisioningError("S3 task must register exactly worker-a and worker-b scopes")
        workers: dict[str, WorkerResult] = {}
        turns: dict[str, Any] = {}
        envelopes: dict[str, Any] = {}

        def run_worker(worker_id: str, root: Path) -> tuple[str, WorkerResult, Any, Any]:
            before = _snapshot(root)
            executor = self.executor_factory(worker_id, root)
            if executor is None:
                raise S3ProvisioningError(f"no empirical executor provisioned for {worker_id}")
            scope = task["worker_scopes"][worker_id]
            prompt = (
                f"Work as {worker_id} on the registered coordination task {task['id']}. "
                f"Your exclusive scope is {json.dumps(scope, sort_keys=True)}. "
                "Make only the necessary change in that scope, run relevant tests, and return "
                "JSON with worker_id, completed, evidence, and optional logical_task."
            )
            turn = executor.run(
                prompt,
                capture_name=capture_name(worker_id),
                study_id=self.study_id,
                case_id=f"{task['id']}:{worker_id}",
                condition=condition,
                fixture=str(task.get("fixture", task["id"])),
                fixture_version=fixture_version,
                repetition=repetition,
                seed=seed,
                workspace_identity=workspace_identity(worker_id, root),
                auxiliary_calls=(),
            )
            structured = turn.final_output
            if not isinstance(structured, dict) or structured.get("worker_id") != worker_id:
                raise S3ProvisioningError(f"{worker_id} returned a mismatched structured identity")
            after = _snapshot(root)
            changed = sorted(set(before) | set(after))
            events = tuple(
                CoordinationEvent(
                    worker_id=worker_id,
                    event="edit",
                    path=path,
                    content_digest=after.get(path),
                    logical_task=structured.get("logical_task") if isinstance(structured.get("logical_task"), str) else None,
                    sequence=index,
                )
                for index, path in enumerate(path for path in changed if before.get(path) != after.get(path))
            )
            success = bool(self.objective_evaluator(task, worker_id, root, structured))
            result = WorkerResult(worker_id, success, events, None if success else "objective_postcondition_failed")
            score_observation = {
                "worker_id": worker_id,
                "success": success,
                "changed_paths": [event.path for event in events],
                "git_status": _git_status(root),
            }
            envelope = turn.to_envelope(
                study_id=self.study_id,
                case_id=f"{task['id']}:{worker_id}",
                condition=condition,
                fixture=str(task.get("fixture", task["id"])),
                fixture_version=fixture_version,
                repetition=repetition,
                seed=seed,
                scorer_version=SCORER_VERSION,
                success=success,
                observations=score_observation,
            )
            return worker_id, result, turn, envelope

        # The existing context manager creates matched copies; separate executor
        # instances ensure separate public app-server threads and capture paths.
        from concurrent.futures import ThreadPoolExecutor
        with isolated_worker_worktrees(source) as roots:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(run_worker, worker_id, roots[worker_id]) for worker_id in ("worker-a", "worker-b")]
                for future in futures:
                    worker_id, result, turn, envelope = future.result()
                    workers[worker_id], turns[worker_id], envelopes[worker_id] = result, turn, envelope
        ordered_workers = tuple(workers[worker_id] for worker_id in ("worker-a", "worker-b"))
        ordered_turns = tuple(turns[worker_id] for worker_id in ("worker-a", "worker-b"))
        ordered_envelopes = tuple(envelopes[worker_id] for worker_id in ("worker-a", "worker-b"))
        return S3EmpiricalResult(task["id"], condition, score_coordination(task, list(ordered_workers)), ordered_workers, ordered_turns, ordered_envelopes)
