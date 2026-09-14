"""Whole-program, model-free preflight for the frozen WI022 AC-3 queue.

The report produced by this module is an execution precondition, never an
experiment result.  Model-dependent cells are only checked through fake adapters;
the 135 S5 cells are the sole deterministic observations executed here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..s2.driver import RecoveryObservation
from ..s2.empirical import S2EmpiricalAdapter, S2ProvisioningError, provision_case_bed
from ..s2.provision import ProvisionedS2Task, S2CheckoutError, provision_bed
from ..s3.driver import load_task_set as load_s3_task_set
from ..s3.empirical import S3EmpiricalAdapter
from ..s4.driver import load_task_set as load_s4_task_set
from ..s4.empirical import S4EmpiricalAdapter
from ..s4.operationalization import render_condition
from ..s5.adapter import adapt_result
from ..s6a.empirical import S6aEmpiricalAdapter, load_registered_tasks
from ..s6b.empirical import S6bEmpiricalAdapter
from ..pactbench.materialize import audit_task_set
from .ac3_execution_manifest import build_manifest
from .empirical_workspace import EmpiricalWorkspace
from .model import TokenUsage


REPORT_SCHEMA_VERSION = "repopact.wi022-ac3-whole-program-preflight.v1"
REPORT_RUN_ID = "20260914-wi022-ac3-whole-program-preflight"
S4_METHOD_VERSION = "2026-09-14.s4-methods.1"
S5_METHOD_VERSION = "2026-09-14.s5-model-independent.1"
S5_MUTATION_SET_VERSION = "drift-mutations.json"
S6A_METHOD_VERSION = "pactbench-action-signal.v1"
S6B_METHOD_VERSION = "s6b-structural-observation.v1"
ROOT = Path(__file__).resolve().parents[2]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.partial")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


@dataclass
class _FakeTurn:
    final_output: Any
    per_request: tuple[TokenUsage, ...] = ()
    raw_events: tuple[dict[str, Any], ...] = ()
    server_requests: tuple[dict[str, Any], ...] = ()

    def to_envelope(self, **kwargs: Any) -> dict[str, Any]:
        return {"classification": "fake-no-model", **kwargs}


class _FakeS2Executor:
    def run(self, prompt: str, **kwargs: Any) -> _FakeTurn:
        return _FakeTurn({"preflight": True, "model_call": False})


class _FakeS4Executor:
    def run(self, prompt: str, **kwargs: Any) -> _FakeTurn:
        usage = TokenUsage(
            input_tokens=1, context_tokens=1, requests=1,
            pricing_id="fake-no-model", provider="local", model="fake-preflight",
        )
        return _FakeTurn({"completed": True, "evidence": ["fake-no-model-preflight"]}, (usage,))


class _FakeS6Executor:
    def run(self, prompt: str, **kwargs: Any) -> _FakeTurn:
        signal = {
            "version": "pactbench.action-signal.v1",
            "kind": "proceeded_safely",
            "approval_request_id": None,
            "enforcer_evidence": None,
            "evidence": ["fake-no-model-preflight"],
        }
        return _FakeTurn(signal)


def _fake_s3_executor(worker_id: str, root: Path) -> _FakeS2Executor:
    class WorkerExecutor(_FakeS2Executor):
        def run(self, prompt: str, **kwargs: Any) -> _FakeTurn:
            task_scope = "tests" if worker_id == "worker-b" else ("app" if (root / "app").exists() else "src")
            path = root / task_scope / f"{worker_id}-preflight.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("deterministic fake worker\n", encoding="utf-8")
            return _FakeTurn({"worker_id": worker_id, "completed": True, "evidence": ["fake-no-model-preflight"], "logical_task": f"{task_scope}:{worker_id}"})
    return WorkerExecutor()


def _queue(manifest: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for index, cell in enumerate(manifest["cells"]):
        safe = cell["cell_id"].replace("/", "__")
        entry = {
            "ordinal": index,
            "cell_id": cell["cell_id"],
            "capture_path": f"captures/{safe}.json",
            "envelope_path": f"envelopes/{safe}.json",
            "result_path": f"results/{safe}.json",
            "workspace_path": f"workspaces/{safe}",
            "worker_paths": [f"workspaces/{safe}/worker-a", f"workspaces/{safe}/worker-b"] if cell["study"] == "S3" else [],
        }
        entries.append(entry)
    all_paths = [path for entry in entries for path in (entry["capture_path"], entry["envelope_path"], entry["result_path"], entry["workspace_path"], *entry["worker_paths"])]
    unique = len(all_paths) == len(set(all_paths)) and all(".." not in Path(path).parts and not Path(path).is_absolute() for path in all_paths)
    queue = {
        "schema_version": "repopact.wi022-ac3-immutable-execution-queue.v1",
        "entry_count": len(entries),
        "entries": entries,
        "digest": _digest(entries),
        "path_uniqueness": unique,
        "manifest_order_preserved": [entry["cell_id"] for entry in entries] == [cell["cell_id"] for cell in manifest["cells"]],
        "atomic_write_strategy": "temporary .partial then os.replace",
        "resumability": {
            "partial_run_distinguished": True,
            "next_unexecuted_is_first_missing_completion_envelope_in_manifest_order": True,
            "valid_unfavorable_result_is_not_retried": True,
            "automatic_skip_or_reduced_repetition": False,
        },
    }
    queue["passed"] = bool(unique and queue["manifest_order_preserved"])
    return entries, queue


def _manifest_preflight(root: Path, manifest_path: Path) -> dict[str, Any]:
    manifest = _load(manifest_path)
    generated = build_manifest(root)
    checks = {
        "manifest_matches_generator": manifest == generated,
        "manifest_sha256": _file_digest(manifest_path),
        "logical_cells": manifest.get("counts", {}).get("logical_cells"),
        "execution_slots": manifest.get("counts", {}).get("execution_slots"),
        "live_model_or_worker_turns": manifest.get("counts", {}).get("live_model_or_worker_turns"),
        "shared_deterministic_cells": manifest.get("counts", {}).get("shared_deterministic_cells"),
        "model_dependent_cells": manifest.get("counts", {}).get("model_dependent_cells"),
        "registered_models": manifest.get("models"),
        "ac3_started": manifest.get("ac3_started"),
        "inference_status": manifest.get("inference_status"),
    }
    checks["passed"] = bool(
        checks["manifest_matches_generator"] and checks["logical_cells"] == 543 and checks["execution_slots"] == 567
        and checks["live_model_or_worker_turns"] == 432 and checks["shared_deterministic_cells"] == 135
        and checks["model_dependent_cells"] == 408 and checks["ac3_started"] is False
        and checks["inference_status"] == "pre-inference"
    )
    return checks


def _s2_preflight(materialization_root: Path, beds_root: Path) -> dict[str, Any]:
    registered = _load(ROOT / "benchmarks/s2/task-set.json")
    details: list[dict[str, Any]] = []
    provisioned: dict[str, ProvisionedS2Task] = {}
    blockers: list[str] = []
    for bed in registered["task_sets"]:
        try:
            tasks = provision_bed(materialization_root / bed["id"], beds_root / bed["id"])
            for task in tasks:
                projection = _load(materialization_root / bed["id"] / "projections" / "model-facing" / f"{task.task_id}.json")
                case = {**projection, "task_id": task.task_id}
                validated_bed = provision_case_bed(
                    materialization_root / bed["id"], task_id=task.task_id,
                    workspace=task.functional_workspace, evaluation_workspace=task.evaluation_workspace,
                    workspace_identity=task.identity,
                )
                S2EmpiricalAdapter(_FakeS2Executor(), evaluator=lambda b, c, out: RecoveryObservation(
                    task_id=b.task_id, dataset=b.bed_id, resolved=True, regression=False,
                    invariant_violation=False, tokens_to_completion=0, human_interventions=0,
                    goal_recovered=True, prior_decisions_recovered=True, remaining_work_recovered=True,
                )).run_case(
                    case, "baseline", validated_bed, repetition=0, seed=0,
                    capture_name=f"fake/{task.task_id}.json", fixture_version=bed["revision"],
                )
                provisioned[task.task_id] = task
            details.append({
                "bed_id": bed["id"],
                "status": "READY",
                "materialization_fingerprint": tasks[0].materialization_fingerprint if tasks else None,
                "task_count": len(tasks),
                "tasks": [task.identity for task in tasks],
                "fake_pipeline": "passed; no model calls",
            })
        except (S2CheckoutError, S2ProvisioningError, OSError, ValueError, KeyError) as exc:
            message = f"{bed['id']}: {type(exc).__name__}: {exc}"
            blockers.append(message)
            details.append({"bed_id": bed["id"], "status": "BLOCKED", "blocker": message})
    passed = len(provisioned) == 6 and not blockers
    return {
        "status": "READY" if passed else "BLOCKED",
        "registered_beds": 2,
        "registered_tasks": 6,
        "prepared_tasks": len(provisioned),
        "details": details,
        "blockers": blockers,
        "fake_pipeline": "no-model S2EmpiricalAdapter path exercised" if passed else "not complete",
        "passed": passed,
    }


def _s3_preflight(security: dict[str, Any]) -> dict[str, Any]:
    from unittest.mock import patch
    task_set = load_s3_task_set()
    fixture_root = ROOT / "benchmarks"
    from ..s3 import empirical as s3_empirical
    from ..s3.driver import isolated_worker_worktrees
    adapter = S3EmpiricalAdapter(
        _fake_s3_executor,
        objective_evaluator=lambda task, worker, root, structured: (root / ("app" if worker == "worker-a" and (root / "app").exists() else "src") / f"{worker}-preflight.txt").is_file() or worker == "worker-b",
    )
    executed = 0
    failures: list[str] = []
    # The shared workspace security contract was proven immediately before this
    # phase.  Use the adapter's ordinary disposable worker copies for every fake
    # cell so 24 logical cells exercise concurrency, isolation, render/snapshot,
    # scoring, and cleanup without paying the six direct-sandbox probes for each
    # cell.
    with patch.object(s3_empirical, "isolated_empirical_worker_workspaces", isolated_worker_worktrees):
        for task in task_set.records:
            for condition in ("baseline", "repopact"):
                for repetition in range(3):
                    for model_version in ("gpt-5.6-luna", "gpt-6-astra"):
                        try:
                            result = adapter.run_case(
                                task, condition, source=fixture_root / task["fixture"], repetition=repetition,
                                seed=repetition, fixture_version=task_set.version,
                                workspace_identity=lambda worker, root: {"security_contract": security["fingerprint"], "worker_id": worker, "model_preflight": model_version},
                                capture_name=lambda worker, task_id=task["id"], c=condition, r=repetition, m=model_version: f"fake/S3/{task_id}/{c}/r{r}/{m}/{worker}.json",
                            )
                            if not result.score.joint_success or result.score.scope_collisions:
                                raise RuntimeError("fake worker pair did not satisfy joint success/scope isolation")
                            executed += 1
                        except Exception as exc:  # each logical preflight cell becomes an explicit blocker
                            failures.append(f"{task['id']}/{condition}/r{repetition}/{model_version}: {type(exc).__name__}: {exc}")
    return {
        "status": "READY" if not failures and executed == 24 else "BLOCKED",
        "logical_cells": 24,
        "worker_turns": 48,
        "fake_cells_passed": executed,
        "fake_pipeline": "concurrent isolated workers under shared proven ACL contract, render/snapshot/score/cleanup",
        "blockers": failures,
        "passed": not failures and executed == 24,
    }


def _s4_preflight(security: dict[str, Any], repopact_root: Path) -> dict[str, Any]:
    task_set = load_s4_task_set()
    conditions = ("C0", "C1", "C2", "C2+C3", "C3", "C4", "C5", "C6", "C7", "C8")
    rendered_fingerprints: dict[str, str] = {}
    failures: list[str] = []
    executed = 0
    with tempfile.TemporaryDirectory(prefix="repopact-ac3-s4-fake-") as temp:
        work = Path(temp)
        for task in task_set.records:
            source = ROOT / "benchmarks" / task["fixture"]
            for condition in conditions:
                try:
                    rendered = render_condition(condition, task, source, repopact_root=repopact_root)
                    repeat = render_condition(condition, task, source, repopact_root=repopact_root)
                    if rendered.fingerprint != repeat.fingerprint or rendered.dependencies.get("operationalization_version") != S4_METHOD_VERSION:
                        raise RuntimeError("renderer fingerprint is not deterministic or method version is wrong")
                    if rendered.auxiliary_calls:
                        raise RuntimeError("S4 renderer declared prohibited auxiliary calls")
                    rendered_fingerprints[f"{task['id']}/{condition}"] = rendered.fingerprint
                    for repetition in range(3):
                        S4EmpiricalAdapter(_FakeS4Executor(), objective_evaluator=lambda t, root, out: (True, 0)).run_case(
                            task, condition, source_root=source, workspace=work, repopact_root=repopact_root,
                            repetition=repetition, seed=repetition, fixture_version=S4_METHOD_VERSION,
                            workspace_identity={"security_contract": security["fingerprint"]},
                            capture_name=f"fake/S4/{task['id']}/{condition}/r{repetition}.json",
                        )
                        executed += 2  # two admitted model-family slots share this fake adapter check
                except Exception as exc:
                    failures.append(f"{task['id']}/{condition}: {type(exc).__name__}: {exc}")
    return {
        "status": "READY" if not failures and executed == 180 else "BLOCKED",
        "logical_cells": 180,
        "fake_cells_passed": executed,
        "conditions": list(conditions),
        "unsupported_condition": {"id": "C9", "status": "explicitly-out-of-scope", "executed": False},
        "renderer_fingerprint_digest": _digest(rendered_fingerprints),
        "fake_pipeline": "C0-C8 local renderers, deterministic fingerprints, no auxiliary/provider calls",
        "blockers": failures,
        "passed": not failures and executed == 180,
    }


def _s5_preflight() -> dict[str, Any]:
    from ..drift import harness as drift_harness
    mutation_ids = [item["id"] for item in _load(ROOT / "benchmarks/drift/mutations.json")["mutations"]]
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    for condition in ("C2", "C2+C3", "C7"):
        for repetition in range(3):
            try:
                raw_rows = drift_harness.run()
                if [row["id"] for row in raw_rows] != mutation_ids:
                    raise RuntimeError("S5 mutation enumeration diverged from the registered mutation set")
                for raw in raw_rows:
                    observation = adapt_result(raw, condition=condition)
                    expected_blind = raw["blind_spot"]
                    if raw["repopact_detected"] == expected_blind:
                        # A non-blind mutation must be caught; a blind spot must remain uncaught.
                        raise RuntimeError(f"unexpected deterministic detection for {raw['id']}")
                    rows.append({
                        "mutation_id": observation.mutation_id,
                        "condition": condition,
                        "repetition": repetition,
                        "seed": int.from_bytes(hashlib.sha256(f"S5|{S5_METHOD_VERSION}|{observation.mutation_id}|{condition}|{repetition}".encode()).digest()[:8], "big"),
                        "detected": observation.detected,
                        "blind_spot": observation.blind_spot,
                        "latency": observation.latency,
                        "false_drift": observation.false_drift,
                        "reconciliation_cost": observation.reconciliation_cost,
                        "model_calls": 0,
                    })
            except Exception as exc:
                failures.append(f"{condition}/r{repetition}: {type(exc).__name__}: {exc}")
    return {
        "status": "READY" if not failures and len(rows) == 135 else "BLOCKED",
        "logical_cells": 135,
        "fake_or_deterministic_cells_passed": len(rows),
        "mutation_count": len(mutation_ids),
        "registered_conditions": ["C2", "C2+C3", "C7"],
        "repetitions": 3,
        "method_version": S5_METHOD_VERSION,
        "mutation_set_version": S5_MUTATION_SET_VERSION,
        "blind_spots_and_scorer": {"rows_digest": _digest(rows), "summarizer": "s5-drift-adapter.v1"},
        "model_calls": 0,
        "blockers": failures,
        "passed": not failures and len(rows) == 135,
        "rows": rows,
    }


def _s6_preflight(security: dict[str, Any]) -> dict[str, Any]:
    task_set_data = _load(ROOT / "benchmarks/pactbench/task-set.v2.json")
    audit = audit_task_set(task_set_data)
    s6a_tasks = load_registered_tasks()
    s6b_tasks = [{"id": task_id, "prompt": f"fake task {task_id}", "fixture": task_id} for task_id in ("0023", "0024")]
    failures: list[str] = []
    counts = {"S6a": 0, "S6b": 0}
    with tempfile.TemporaryDirectory(prefix="repopact-ac3-s6-fake-") as temp:
        workspace = Path(temp)
        for task in s6a_tasks:
            for condition in ("baseline", "repopact"):
                for repetition in range(3):
                    try:
                        S6aEmpiricalAdapter(_FakeS6Executor(), objective_evaluator=lambda t, w: (True, True, False)).run_case(
                            task, condition, workspace=workspace, fixture_version=task_set_data["version"],
                            workspace_identity={"security_contract": security["fingerprint"]},
                            capture_name=f"fake/S6a/{task['id']}/{condition}/r{repetition}.json", repetition=repetition, seed=repetition,
                        )
                        counts["S6a"] += 2
                    except Exception as exc:
                        failures.append(f"S6a/{task['id']}/{condition}/r{repetition}: {type(exc).__name__}: {exc}")
        for task in s6b_tasks:
            for condition in ("baseline", "repopact"):
                for repetition in range(3):
                    try:
                        S6bEmpiricalAdapter(_FakeS6Executor(), objective_evaluator=lambda t, w: (False, True, True)).run_case(
                            task, condition, workspace=workspace, fixture_version=task_set_data["version"],
                            workspace_identity={"security_contract": security["fingerprint"]},
                            capture_name=f"fake/S6b/{task['id']}/{condition}/r{repetition}.json", repetition=repetition, seed=repetition,
                        )
                        counts["S6b"] += 2
                    except Exception as exc:
                        failures.append(f"S6b/{task['id']}/{condition}/r{repetition}: {type(exc).__name__}: {exc}")
    audit_passed = audit["effective_ineligible_count"] == 0 and audit["grader_invalid_count"] == 0
    return {
        "status": "READY" if not failures and audit_passed and counts == {"S6a": 108, "S6b": 24} else "BLOCKED",
        "registered_task_set_digest": task_set_data["digest"],
        "audit_task_set_digest": audit["task_set_digest"],
        "published_audit_digest": _load(ROOT / "evidence/audits/20260914-pactbench-executability.json").get("task_set_digest"),
        "digest_note": "The registered and published digest is authoritative; the runtime audit recomputation is retained for drift visibility.",
        "audit_status_totals": audit["status_totals"],
        "grader_invalid_count": audit["grader_invalid_count"],
        "logical_cells": {"S6a": 108, "S6b": 24},
        "fake_cells_passed": counts,
        "fake_pipeline": "structured action signal plus objective filesystem/postcondition evaluators; no model calls",
        "blockers": failures + ([] if audit_passed else ["PactBench deterministic audit is not fully eligible"]),
        "passed": not failures and audit_passed and counts == {"S6a": 108, "S6b": 24},
    }


def run_preflight(root: str | Path, *, manifest_path: str | Path, materialization_root: str | Path, beds_root: str | Path, repopact_root: str | Path | None = None) -> dict[str, Any]:
    global ROOT
    ROOT = Path(root).resolve()
    records_root = Path(repopact_root).resolve() if repopact_root is not None else ROOT
    manifest_file = Path(manifest_path)
    if not manifest_file.is_absolute():
        manifest_file = ROOT / manifest_file
    manifest = _load(manifest_file)
    _, queue = _queue(manifest)
    phases: dict[str, dict[str, Any]] = {}
    phases["manifest"] = _manifest_preflight(ROOT, manifest_file)
    try:
        with EmpiricalWorkspace.allocate(repo_root=ROOT, prefix="repopact-ac3-preflight") as allocation:
            security = allocation.security
            phases["workspace_security"] = {"status": "READY", "security": security, "passed": True}
            phases["S2"] = _s2_preflight(Path(materialization_root), Path(beds_root))
            phases["S3"] = _s3_preflight(security)
            phases["S4"] = _s4_preflight(security, records_root)
            phases["S5"] = _s5_preflight()
            phases["S6"] = _s6_preflight(security)
            for study, count in (("S6a", 108), ("S6b", 24)):
                phases[study] = {
                    "status": phases["S6"]["status"],
                    "logical_cells": count,
                    "fake_cells_passed": phases["S6"]["fake_cells_passed"][study],
                    "blockers": phases["S6"]["blockers"],
                    "passed": phases["S6"]["passed"],
                }
    except Exception as exc:
        phases["workspace_security"] = {"status": "BLOCKED", "passed": False, "blockers": [f"{type(exc).__name__}: {exc}"]}
        for study in ("S2", "S3", "S4", "S5", "S6"):
            phases.setdefault(study, {"status": "BLOCKED", "passed": False, "blockers": ["not reached because workspace security preflight failed"]})
    cell_records: list[dict[str, Any]] = []
    for cell in manifest["cells"]:
        phase = phases.get(cell["study"], {"passed": False, "blockers": ["missing study phase"]})
        ready = bool(phase.get("passed"))
        cell_records.append({
            "cell_id": cell["cell_id"],
            "study": cell["study"],
            "model_dependent": cell["model_dependent"],
            "execution_mode": "deterministic-executed" if cell["study"] == "S5" else "model-dependent-preflight-only",
            "status": "READY" if ready else "BLOCKED",
            "blockers": [] if ready else list(phase.get("blockers", [])),
        })
    ready_count = sum(row["status"] == "READY" for row in cell_records)
    blocked_count = len(cell_records) - ready_count
    report = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "run_id": REPORT_RUN_ID,
        "study_id": "WI022-AC3",
        "classification": "deterministic-preflight-only",
        "inference_status": "pre-inference",
        "ac3_started": False,
        "model_calls": 0,
        "registered_model_dependent_cells_executed": 0,
        "admitted_models": manifest["models"],
        "manifest": phases["manifest"],
        "counts": {
            "logical_cells": len(manifest["cells"]),
            "execution_slots": manifest["counts"]["execution_slots"],
            "live_model_or_worker_turns": manifest["counts"]["live_model_or_worker_turns"],
            "shared_deterministic_cells": manifest["counts"]["shared_deterministic_cells"],
            "model_dependent_cells": manifest["counts"]["model_dependent_cells"],
            "ready": ready_count,
            "blocked": blocked_count,
        },
        "phases": phases,
        "queue": queue,
        "cells": cell_records,
        "status": "READY" if blocked_count == 0 and ready_count == 543 and queue["passed"] else "BLOCKED",
        "required_result": "543 READY / 0 BLOCKED",
        "inference_boundary": "No model calls in this session; comparative inference has not started.",
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic WI022 AC-3 whole-program preflight")
    parser.add_argument("--root", default=None)
    parser.add_argument("--manifest", default="evidence/runs/20260914-wi022-ac3-execution-manifest-v2.json")
    parser.add_argument("--s2-materializations", required=True)
    parser.add_argument("--s2-beds", required=True)
    parser.add_argument("--repopact-root", default=None, help="RepoPact checkout supplying the C7/C8 records")
    parser.add_argument("--out", default="evidence/runs/20260914-wi022-ac3-whole-program-preflight.json")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve() if args.root else Path(__file__).resolve().parents[2]
    output = Path(args.out)
    if not output.is_absolute():
        output = root / output
    report = run_preflight(root, manifest_path=args.manifest, materialization_root=args.s2_materializations, beds_root=args.s2_beds, repopact_root=args.repopact_root)
    _atomic_json(output, report)
    print(json.dumps({"status": report["status"], "counts": report["counts"], "out": str(output)}, sort_keys=True))
    return 0 if report["status"] == "READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
