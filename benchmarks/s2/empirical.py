"""S2 empirical adapter boundary for pinned SWE-bench/SWE-EVO material."""
from __future__ import annotations

import json
import hashlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .driver import RecoveryObservation, SCORER_VERSION, score_recovery
from .materialize import MaterializationError, verify_materialization


RECOVERY_INSTRUCTION_VERSION = "s2-recovery-instruction.v1"
RECOVERY_INSTRUCTION = (
    "Continue the registered task from the supplied repository state. Reconstruct the goal, "
    "prior decisions, and remaining work from the available task material. Make the smallest "
    "correct change, run the registered tests, and report evidence in the required JSON shape."
)


class S2ProvisioningError(RuntimeError):
    """Pinned material exists but a functional/evaluation bed is not provisioned."""


@dataclass(frozen=True)
class S2CaseBed:
    task_id: str
    bed_id: str
    materialization_dir: Path
    workspace: Path
    evaluation_workspace: Path
    workspace_identity: dict[str, Any]


@dataclass(frozen=True)
class MatchedS2Workspaces:
    baseline: Path
    repopact: Path
    functional_seed_digest: str


def _tree_digest(root: Path) -> str:
    rows: list[tuple[str, str]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        if path.is_file() and ".git" not in path.relative_to(root).parts:
            rows.append((path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest()))
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def create_matched_workspaces(functional_seed: str | Path, destination_root: str | Path) -> MatchedS2Workspaces:
    """Copy one study-built functional checkout into identical baseline/repoPact roots."""
    source = Path(functional_seed)
    if not source.is_dir() or not any(source.iterdir()):
        raise S2ProvisioningError("S2 functional seed checkout is missing or empty")
    destination = Path(destination_root)
    destination.mkdir(parents=True, exist_ok=True)
    baseline = destination / "baseline"
    repopact = destination / "repopact"
    if baseline.exists() or repopact.exists():
        raise S2ProvisioningError("S2 matched workspace destination must be unused")
    shutil.copytree(source, baseline)
    shutil.copytree(source, repopact)
    baseline_digest = _tree_digest(baseline)
    repopact_digest = _tree_digest(repopact)
    if baseline_digest != repopact_digest:
        raise S2ProvisioningError("S2 baseline and RepoPact functional seeds diverged during copy")
    return MatchedS2Workspaces(baseline, repopact, baseline_digest)


@dataclass(frozen=True)
class S2EmpiricalResult:
    task_id: str
    condition: str
    observation: RecoveryObservation
    scored: dict[str, Any]
    turn: Any
    envelope: Any


def provision_case_bed(materialization_dir: str | Path, *, task_id: str, workspace: str | Path, evaluation_workspace: str | Path | None = None, workspace_identity: dict[str, Any] | None = None) -> S2CaseBed:
    """Validate the deterministic materialization and require real workspaces.

    ``materialize.py`` intentionally creates model/evaluation projections rather
    than pretending to be a functional checkout.  The empirical adapter therefore
    fails explicitly until the study-owned checkout builder supplies both roots.
    """
    materialized = Path(materialization_dir)
    try:
        manifest = verify_materialization(materialized)
    except (MaterializationError, OSError, json.JSONDecodeError) as exc:
        raise S2ProvisioningError(f"S2 materialization verification failed: {exc}") from exc
    work = Path(workspace)
    evaluation = Path(evaluation_workspace) if evaluation_workspace is not None else work
    if not work.is_dir() or not any(work.iterdir()):
        raise S2ProvisioningError("S2 requires a non-empty functional model workspace")
    if not evaluation.is_dir() or not any(evaluation.iterdir()):
        raise S2ProvisioningError("S2 requires a non-empty evaluation workspace")
    selected = manifest.get("selected_task_ids", [])
    if task_id not in selected:
        raise S2ProvisioningError(f"task {task_id} is not in the verified S2 materialization")
    if not workspace_identity:
        raise S2ProvisioningError("S2 requires immutable workspace identity metadata")
    return S2CaseBed(
        task_id=task_id,
        bed_id=str(manifest["bed_id"]),
        materialization_dir=materialized,
        workspace=work,
        evaluation_workspace=evaluation,
        workspace_identity=workspace_identity,
    )


class S2EmpiricalAdapter:
    study_id = "S2"

    def __init__(self, executor: Any, *, evaluator: Callable[[S2CaseBed, dict[str, Any], Any], RecoveryObservation]) -> None:
        self.executor = executor
        self.evaluator = evaluator

    def run_case(
        self,
        case: dict[str, Any],
        condition: str,
        bed: S2CaseBed,
        *,
        repetition: int = 0,
        seed: int = 0,
        capture_name: str,
        fixture_version: str,
    ) -> S2EmpiricalResult:
        if bed.task_id != case.get("task_id"):
            raise S2ProvisioningError("S2 case and functional bed task ids do not match")
        problem = case.get("problem_statement") or case.get("prompt")
        if not isinstance(problem, str) or not problem.strip():
            raise S2ProvisioningError("S2 model-facing case lacks a registered problem statement")
        prompt = f"{problem.rstrip()}\n\n{RECOVERY_INSTRUCTION}"
        turn = self.executor.run(
            prompt,
            capture_name=capture_name,
            study_id=self.study_id,
            case_id=bed.task_id,
            condition=condition,
            fixture=bed.bed_id,
            fixture_version=fixture_version,
            repetition=repetition,
            seed=seed,
            workspace_identity=bed.workspace_identity,
            auxiliary_calls=(),
        )
        observation = self.evaluator(bed, case, turn.final_output)
        if not isinstance(observation, RecoveryObservation) or observation.task_id != bed.task_id:
            raise TypeError("S2 evaluator must return a matching RecoveryObservation")
        scored = score_recovery(observation)
        envelope = turn.to_envelope(
            study_id=self.study_id,
            case_id=bed.task_id,
            condition=condition,
            fixture=bed.bed_id,
            fixture_version=fixture_version,
            repetition=repetition,
            seed=seed,
            scorer_version=SCORER_VERSION,
            success=observation.resolved,
            observations=scored,
        )
        return S2EmpiricalResult(bed.task_id, condition, observation, scored, turn, envelope)
