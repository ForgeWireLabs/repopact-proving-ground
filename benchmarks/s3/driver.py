"""Concurrent S3 orchestration and deterministic coordination scoring."""
from __future__ import annotations

import copy
import fnmatch
import json
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

try:
    from ..harness.registry import RegisteredSet, load_registered_set
except ImportError:  # pragma: no cover
    from harness.registry import RegisteredSet, load_registered_set  # type: ignore


SCORER_VERSION = "s3-coordination-rubric.v1"


@dataclass(frozen=True)
class CoordinationEvent:
    worker_id: str
    event: str
    path: str | None = None
    content_digest: str | None = None
    logical_task: str | None = None
    sequence: int = 0


@dataclass(frozen=True)
class WorkerResult:
    worker_id: str
    success: bool
    events: tuple[CoordinationEvent, ...] = ()
    failure: str | None = None


@dataclass(frozen=True)
class CoordinationScore:
    conflicting_edits: int
    duplicated_work: int
    scope_collisions: int
    joint_success: bool
    conflict_rate: float
    duplicate_work_rate: float
    scope_collision_rate: float
    scorer_version: str = SCORER_VERSION


def _matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(path, pattern.replace("/**", "/*")) for pattern in patterns)


def score_coordination(task: dict[str, Any], results: list[WorkerResult]) -> CoordinationScore:
    events = [event for result in results for event in result.events]
    edits = [event for event in events if event.event == "edit" and event.path]
    by_path: dict[str, set[str]] = {}
    for event in edits:
        by_path.setdefault(event.path or "", set()).add(event.content_digest or "")
    conflicts = sum(1 for digests in by_path.values() if len(digests) > 1)
    by_logical: dict[str, set[str]] = {}
    for event in events:
        if event.logical_task:
            by_logical.setdefault(event.logical_task, set()).add(event.worker_id)
    duplicate_tasks = sum(1 for workers in by_logical.values() if len(workers) > 1)
    scopes = task.get("worker_scopes", {})
    collisions = sum(
        1 for event in edits
        if not _matches(event.path or "", list(scopes.get(event.worker_id, [])))
    )
    return CoordinationScore(
        conflicting_edits=conflicts,
        duplicated_work=duplicate_tasks,
        scope_collisions=collisions,
        joint_success=bool(results) and all(result.success for result in results),
        conflict_rate=round(conflicts / max(len(by_path), 1), 3),
        duplicate_work_rate=round(duplicate_tasks / max(len(by_logical), 1), 3),
        scope_collision_rate=round(collisions / max(len(edits), 1), 3),
    )


def load_task_set(path: str | Path | None = None) -> RegisteredSet:
    source = Path(path) if path else Path(__file__).with_name("task-set.json")
    registered = load_registered_set(source, study_id="S3", records_key="tasks")
    for task in registered.records:
        if not isinstance(task.get("worker_scopes"), dict) or set(task["worker_scopes"]) != {"worker-a", "worker-b"}:
            raise ValueError(f"S3 task {task.get('id')} must register both worker scopes")
    return registered


@contextmanager
def isolated_worker_worktrees(source: str | Path, worker_ids: tuple[str, ...] = ("worker-a", "worker-b")) -> Iterator[dict[str, Path]]:
    """Create disposable per-worker fixture copies; never let a study mutate its source."""
    source_path = Path(source)
    with tempfile.TemporaryDirectory(prefix="repopact-s3-") as temp:
        roots: dict[str, Path] = {}
        for worker_id in worker_ids:
            destination = Path(temp) / worker_id
            shutil.copytree(source_path, destination)
            roots[worker_id] = destination
        yield roots


class S3Driver:
    study_id = "S3"

    def __init__(self, task_set: RegisteredSet | None = None) -> None:
        self.task_set = task_set or load_task_set()

    def run_concurrently(self, task: dict[str, Any], runner: Any, *, source: str | Path | None = None) -> CoordinationScore:
        roots_context = isolated_worker_worktrees(source) if source else _empty_roots()
        with roots_context as roots:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [
                    pool.submit(runner.run, task, worker_id, roots[worker_id])
                    for worker_id in ("worker-a", "worker-b")
                ]
                results = [future.result() for future in futures]
        if any(not isinstance(result, WorkerResult) for result in results):
            raise TypeError("S3 runner must return WorkerResult")
        return score_coordination(task, results)


@contextmanager
def _empty_roots() -> Iterator[dict[str, Path]]:
    with tempfile.TemporaryDirectory(prefix="repopact-s3-empty-") as temp:
        yield {worker: Path(temp) / worker for worker in ("worker-a", "worker-b")}
