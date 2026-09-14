"""Provision reproducible S2 functional and evaluation checkouts.

This module is a deterministic boundary between the pinned S2 materialization and
the later model runner.  It never applies a candidate/model patch, starts tests, or
invokes a model.  The functional checkout is exactly the registered repository base
commit; the separate evaluator checkout has only the registered test patch applied.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .materialize import MaterializationError, verify_materialization


PROVISIONER_VERSION = "2026-09-14.s2-functional-evaluator-provisioner.1"
EVALUATOR_PROCEDURE_VERSION = "swebench-official-patch-then-test.v1"
_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class S2CheckoutError(RuntimeError):
    """A registered S2 checkout or evaluator counterpart is not trustworthy."""


@dataclass(frozen=True)
class ProvisionedS2Task:
    task_id: str
    bed_id: str
    materialization_fingerprint: str
    record_sha256: str
    repository: str
    base_commit: str
    functional_workspace: Path
    evaluation_workspace: Path
    identity: dict[str, Any]


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _git(args: list[str], *, cwd: Path | None = None, input_bytes: bytes | None = None, timeout: float = 900.0) -> bytes:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None, input=input_bytes,
            capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise S2CheckoutError(f"git command failed: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
        raise S2CheckoutError(f"git {' '.join(args[:3])} failed: {detail}")
    return result.stdout


def _git_text(args: list[str], *, cwd: Path | None = None, timeout: float = 900.0) -> str:
    return _git(args, cwd=cwd, timeout=timeout).decode("utf-8", errors="strict").strip()


def _repo_url(repository: str) -> str:
    if not re.fullmatch(r"[^/\\]+/[^/\\]+", repository):
        raise S2CheckoutError(f"invalid registered repository identity: {repository}")
    return f"https://github.com/{repository}.git"


def _assert_clean_base(path: Path, *, base_commit: str, repository: str) -> dict[str, str]:
    if not (path / ".git").exists():
        raise S2CheckoutError(f"functional/evaluation checkout is not a Git checkout: {path}")
    origin = _git_text(["-C", str(path), "remote", "get-url", "origin"])
    expected_url = _repo_url(repository)
    if origin.rstrip("/").removesuffix(".git") != expected_url.removesuffix(".git"):
        raise S2CheckoutError(f"checkout origin mismatch for {path}: {origin!r}")
    head = _git_text(["-C", str(path), "rev-parse", "HEAD"])
    if head != base_commit:
        raise S2CheckoutError(f"checkout {path} resolved {head}, expected {base_commit}")
    status = _git_text(["-C", str(path), "status", "--porcelain"])
    if status:
        raise S2CheckoutError(f"functional checkout is not clean: {path}")
    tree = _git_text(["-C", str(path), "rev-parse", "HEAD^{tree}"])
    return {"head": head, "tree": tree}


def _clone_at_base(repository: str, base_commit: str, destination: Path) -> dict[str, str]:
    if destination.exists():
        return _assert_clean_base(destination, base_commit=base_commit, repository=repository)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Initialize and fetch the exact commit directly.  This avoids cloning a
    # repository's complete advertised history merely to reach one immutable base.
    _git(["init", "--quiet", str(destination)])
    _git(["-C", str(destination), "remote", "add", "origin", _repo_url(repository)])
    _git(["-C", str(destination), "fetch", "--quiet", "--no-tags", "--depth=1", "--filter=blob:none", "origin", base_commit])
    _git(["-C", str(destination), "checkout", "--quiet", "--detach", "--force", base_commit])
    return _assert_clean_base(destination, base_commit=base_commit, repository=repository)


def _evaluation_checkout(
    repository: str,
    base_commit: str,
    destination: Path,
    test_patch: str,
    *,
    functional_source: Path,
    test_command: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    if not destination.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", "--local", "--no-hardlinks", "--no-tags", str(functional_source), str(destination)])
        _git(["-C", str(destination), "remote", "set-url", "origin", _repo_url(repository)])
        _assert_clean_base(destination, base_commit=base_commit, repository=repository)
        patch_bytes = test_patch.encode("utf-8")
        _git(["-C", str(destination), "apply", "--check", "--whitespace=nowarn"], input_bytes=patch_bytes)
        _git(["-C", str(destination), "apply", "--whitespace=nowarn"], input_bytes=patch_bytes)
    else:
        if not (destination / ".git").exists():
            raise S2CheckoutError(f"evaluation checkout is not a Git checkout: {destination}")
        origin = _git_text(["-C", str(destination), "remote", "get-url", "origin"])
        expected_url = _repo_url(repository)
        if origin.rstrip("/").removesuffix(".git") != expected_url.removesuffix(".git"):
            raise S2CheckoutError(f"evaluation checkout origin mismatch for {destination}: {origin!r}")
        current_head = _git_text(["-C", str(destination), "rev-parse", "HEAD"])
        if current_head != base_commit:
            raise S2CheckoutError(f"evaluation checkout {destination} resolved {current_head}, expected {base_commit}")
        if test_patch:
            _git(["-C", str(destination), "apply", "--reverse", "--check", "--whitespace=nowarn"], input_bytes=test_patch.encode("utf-8"))
        elif _git_text(["-C", str(destination), "status", "--porcelain"]):
            raise S2CheckoutError(f"empty registered test patch has evaluator changes: {destination}")
    # The evaluator counterpart must contain only the registered test patch as its
    # working-tree delta.  A later runner supplies a candidate patch independently.
    diff = _git(["-C", str(destination), "diff", "--binary", "--no-ext-diff"])
    changed_paths = [line for line in _git_text(["-C", str(destination), "diff", "--name-only"]).splitlines() if line]
    if not test_patch and changed_paths:
        raise S2CheckoutError(f"empty registered test patch produced changes: {destination}")
    if test_patch and not changed_paths:
        raise S2CheckoutError(f"registered test patch produced no evaluator changes: {destination}")
    if any(path.startswith("../") or path.startswith("/") or "\\" in path for path in changed_paths):
        raise S2CheckoutError(f"evaluator test patch changed an unsafe path: {changed_paths}")
    evaluator = {
        "procedure_version": EVALUATOR_PROCEDURE_VERSION,
        "candidate_patch_required": True,
        "registered_test_patch_applied": True,
        "registered_test_patch_sha256": _sha256(test_patch.encode("utf-8")),
        "actual_evaluator_diff_sha256": _sha256(diff),
        "changed_paths": changed_paths,
        "test_command": test_command,
        "model_workspace_gold_patch_applied": False,
    }
    base = {"head": _git_text(["-C", str(destination), "rev-parse", "HEAD"]), "tree": _git_text(["-C", str(destination), "rev-parse", "HEAD^{tree}"])}
    return base, evaluator


def _projection(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise S2CheckoutError(f"cannot read S2 projection {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise S2CheckoutError(f"S2 projection is not an object: {path}")
    return value


def provision_bed(
    materialization_dir: str | Path,
    destination_root: str | Path,
    *,
    logical_root: str = "s2-beds",
) -> tuple[ProvisionedS2Task, ...]:
    """Verify one materialized bed and build all of its task checkouts."""
    materialization = Path(materialization_dir).resolve()
    try:
        manifest = verify_materialization(materialization)
    except (MaterializationError, OSError, json.JSONDecodeError) as exc:
        raise S2CheckoutError(f"S2 materialization cannot be provisioned: {exc}") from exc
    destination = Path(destination_root).resolve()
    tasks: list[ProvisionedS2Task] = []
    for selected in manifest["selected_tasks"]:
        task_id = selected["task_id"]
        if not _SAFE_ID.fullmatch(task_id):
            raise S2CheckoutError(f"unsafe registered S2 task id: {task_id}")
        model_path = materialization / selected["model_projection"]
        evaluation_path = materialization / selected["evaluation_projection"]
        model = _projection(model_path)
        evaluation = _projection(evaluation_path)
        if model.get("task_id") != task_id or evaluation.get("task_id") != task_id:
            raise S2CheckoutError(f"projection task mismatch for {task_id}")
        if set(model) != {"schema_version", "study_id", "bed_id", "task_id", "repository", "base_commit", "problem_statement"}:
            raise S2CheckoutError(f"model projection has unexpected or gold-bearing fields for {task_id}")
        if any("patch" in str(key).lower() for key in model):
            raise S2CheckoutError(f"model projection contains a patch field for {task_id}")
        repository = str(selected["repository"])
        base_commit = str(selected["base_commit"])
        task_root = destination / task_id
        functional = task_root / "functional-model"
        evaluator = task_root / "official-evaluator"
        functional_identity = _clone_at_base(repository, base_commit, functional)
        metadata = evaluation.get("source_metadata") or {}
        test_command = str(metadata.get("test_cmds") or "python -m pytest <registered-test-identifiers>")
        evaluator_base, evaluator_identity = _evaluation_checkout(
            repository, base_commit, evaluator, str(evaluation.get("gold_test_patch") or ""),
            functional_source=functional, test_command=test_command,
        )
        identity = {
            "provisioner_version": PROVISIONER_VERSION,
            "logical_root": logical_root,
            "bed_id": manifest["bed_id"],
            "materialization_fingerprint": manifest["fingerprint"],
            "materialized_asset_sha256": manifest["upstream"]["asset_sha256"],
            "task_id": task_id,
            "record_sha256": selected["record_sha256"],
            "repository": repository,
            "base_commit": base_commit,
            "functional_model_workspace": {
                "role": "functional-model-workspace",
                "head": functional_identity["head"],
                "tree": functional_identity["tree"],
                "clean": True,
                "gold_patch_applied": False,
                "gold_test_patch_applied": False,
            },
            "official_evaluator_workspace": {"role": "official-evaluator-workspace", **evaluator_base, **evaluator_identity},
            "model_facing_projection": {
                "path": selected["model_projection"],
                "sha256": _sha256(model_path.read_bytes()),
                "gold_solution_fields_excluded": True,
            },
            "evaluation_projection": {
                "path": selected["evaluation_projection"],
                "sha256": _sha256(evaluation_path.read_bytes()),
                "gold_solution_fields_retained_only_for_evaluator": True,
            },
        }
        tasks.append(ProvisionedS2Task(
            task_id=task_id,
            bed_id=str(manifest["bed_id"]),
            materialization_fingerprint=str(manifest["fingerprint"]),
            record_sha256=str(selected["record_sha256"]),
            repository=repository,
            base_commit=base_commit,
            functional_workspace=functional,
            evaluation_workspace=evaluator,
            identity=identity,
        ))
    return tuple(tasks)


__all__ = [
    "EVALUATOR_PROCEDURE_VERSION",
    "PROVISIONER_VERSION",
    "ProvisionedS2Task",
    "S2CheckoutError",
    "provision_bed",
]
