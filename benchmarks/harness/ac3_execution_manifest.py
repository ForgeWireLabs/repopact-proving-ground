"""Generate the revised WI022 AC-3 plan without executing any benchmark cell."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .registry import deterministic_seed
from ..s4.operationalization import condition_implementation_fingerprint
from ..s5.adapter import condition_implementation_fingerprint as s5_condition_implementation_fingerprint


FROZEN_CORPUS_VERSION = "pactbench-2026-09-14-materialized-v2"
FROZEN_CORPUS_DIGEST = "a9f490fb41cd47b366ff3f3268df92ea24400dd366cdf3f407587e9a5aafed54"
MANIFEST_VERSION = "wi022-ac3-execution-manifest.v3"
SUPERCEDES = "20260914-wi022-ac3-execution-manifest-v2.json"
MODELS = (
    {"family": "gpt-5.6", "provider": "openai", "version": "gpt-5.6-luna"},
    {"family": "claude-sonnet-5", "provider": "anthropic", "version": "claude-sonnet-5"},
)
# Registration digests are frozen from the published checkpoint.  They are kept
# as constants because Windows checkout line-ending conversion must not change a
# task-set identity.
REGISTERED_SOURCE_DIGESTS = {
    "S2": "98d06f1983e3c8bd91e9077bc1d8f388646e8bc00b6a1a3c48173bbd1c17d9d9",
    "S3": "3cdb20c413632930dd543e3e45d72fa3cdbf8319e875380335c4a39e9cfee1",
    "S4": "9a0d596f6f528e24062add8172ca6232059074f07b6053ad43ec320c98eb78c0",
    "S4_conditions": "2d59219fb6975508d91ab51841f23ea17065bee0995ae9727b145ef1937ae1e3",
    "S5": "458572e420306ac8c83cf2c8a01d6d6be2968f67d20a2582912eec8fa6bae6f",
}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _cell(study: str, case_id: str, condition: str, repetition: int, task_version: str, task_digest: str, scorer: str, condition_version: str, *, model: dict[str, str] | None, worker_turns: int, auxiliary_call_class: str, condition_fingerprint: str) -> dict[str, Any]:
    return {
        "cell_id": f"{study}/{case_id}/{condition}/r{repetition}/{model['version'] if model else 'shared-deterministic'}",
        "study": study,
        "case_id": case_id,
        "condition": condition,
        "repetition": repetition,
        "seed": deterministic_seed(study, task_version, case_id, condition, repetition),
        "model": model,
        "model_dependent": model is not None,
        "task_set_version": task_version,
        "task_set_digest": task_digest,
        "condition_version": condition_version,
        "condition_fingerprint": condition_fingerprint,
        "scorer_version": scorer,
        "workspace_materialization_identity": {
            "status": "deterministic-local" if study == "S5" else "requires-study-provisioning",
            "seed_policy": "identical matched functional seed" if study in {"S2", "S3", "S6a", "S6b"} else "condition-specific reset",
            "task_set_digest": task_digest,
        },
        "expected_workers": 2 if study == "S3" else (0 if study == "S5" else 1),
        "worker_turns": worker_turns,
        "auxiliary_call_class": auxiliary_call_class,
        "status": "planned",
    }


def build_manifest(root: str | Path | None = None) -> dict[str, Any]:
    base = Path(root) if root else Path(__file__).resolve().parents[2]
    s2_path = base / "benchmarks/s2/task-set.json"
    s3_path = base / "benchmarks/s3/task-set.json"
    s4_path = base / "benchmarks/s4/task-set.json"
    s4_conditions = base / "benchmarks/s4/conditions.json"
    drift_path = base / "benchmarks/drift/mutations.json"
    pactbench_path = base / "benchmarks/pactbench/task-set.v2.json"
    s2 = _load(s2_path)
    s3 = _load(s3_path)
    s4 = _load(s4_path)
    drift = _load(drift_path)
    pactbench = _load(pactbench_path)
    task_digests = {
        "S2": REGISTERED_SOURCE_DIGESTS["S2"], "S3": REGISTERED_SOURCE_DIGESTS["S3"], "S4": REGISTERED_SOURCE_DIGESTS["S4"],
        "S4_conditions": REGISTERED_SOURCE_DIGESTS["S4_conditions"], "S5": REGISTERED_SOURCE_DIGESTS["S5"],
        "PactBench": FROZEN_CORPUS_DIGEST,
    }
    cells: list[dict[str, Any]] = []
    for record in s2["task_sets"]:
        for task_id in record["task_ids"]:
            for condition in ("baseline", "repopact"):
                for repetition in range(3):
                    for model in MODELS:
                        cells.append(_cell("S2", task_id, condition, repetition, s2["version"], task_digests["S2"], "s2-recovery-rubric.v1", "s2-recovery-instruction.v1", model=model, worker_turns=1, auxiliary_call_class="none", condition_fingerprint="s2-recovery-instruction.v1"))
    for task in s3["tasks"]:
        for condition in ("baseline", "repopact"):
            for repetition in range(3):
                for model in MODELS:
                    cells.append(_cell("S3", task["id"], condition, repetition, s3["version"], task_digests["S3"], "s3-coordination-rubric.v1", "s3-worker-pair.v1", model=model, worker_turns=2, auxiliary_call_class="none", condition_fingerprint="s3-worker-pair.v1"))
    for task in s4["tasks"]:
        for condition in ("C0", "C1", "C2", "C2+C3", "C3", "C4", "C5", "C6", "C7", "C8"):
            for repetition in range(3):
                    for model in MODELS:
                        cells.append(_cell("S4", task["id"], condition, repetition, s4["version"], task_digests["S4"], "s4-token-economy.v1", "2026-09-14.s4-methods.1", model=model, worker_turns=1, auxiliary_call_class="deterministic-local-only", condition_fingerprint=condition_implementation_fingerprint(condition)))
    mutation_ids = [item["id"] for item in drift["mutations"]]
    for mutation_id in mutation_ids:
        for condition in ("C2", "C2+C3", "C7"):
            for repetition in range(3):
                cells.append(_cell("S5", mutation_id, condition, repetition, "drift-mutations.json", task_digests["S5"], "s5-drift-adapter.v1", "2026-09-14.s5-model-independent.1", model=None, worker_turns=0, auxiliary_call_class="deterministic-validator", condition_fingerprint=s5_condition_implementation_fingerprint(condition)))
    # The frozen security-invariant slice is nine cases; the older registry also
    # contains the correctness cases, which are deliberately excluded here.
    security_tasks = ["0002", "0007", "0009", "0011", "0016", "0017", "0019", "0029", "0030"]
    for task_id in security_tasks:
        for condition in ("baseline", "repopact"):
            for repetition in range(3):
                for model in MODELS:
                    cells.append(_cell("S6a", task_id, condition, repetition, FROZEN_CORPUS_VERSION, task_digests["PactBench"], "pactbench-grader.v2", "pactbench-action-signal.v1", model=model, worker_turns=1, auxiliary_call_class="none", condition_fingerprint="pactbench-action-signal.v1"))
    for task_id in ("0023", "0024"):
        for condition in ("baseline", "repopact"):
            for repetition in range(3):
                for model in MODELS:
                    cells.append(_cell("S6b", task_id, condition, repetition, FROZEN_CORPUS_VERSION, task_digests["PactBench"], "s6b-injection-rubric.v1", "s6b-structural-observation.v1", model=model, worker_turns=1, auxiliary_call_class="none", condition_fingerprint="s6b-structural-observation.v1"))
    # The old manifest calls an S3 case/condition/repetition one logical cell and
    # separately counts its two worker turns.  With that same definition, removing
    # the duplicated 135-cell S5 label removes 135 cells from 678: 543 logical cells.
    # The often-quoted 567 is the execution-slot count (432 live turns + 135 S5
    # observations), not the logical-cell count.
    if len(cells) != 543:
        raise AssertionError(f"revised AC-3 plan must contain 543 logical cells, found {len(cells)}")
    by_study = {study: sum(item["study"] == study for item in cells) for study in ("S2", "S3", "S4", "S5", "S6a", "S6b")}
    live_turns = sum(item["worker_turns"] for item in cells if item["model_dependent"])
    return {
        "manifest_version": MANIFEST_VERSION,
        "supersedes": SUPERCEDES,
        "study_id": "WI022-AC3",
        "ac3_started": False,
        "inference_status": "pre-inference",
        "models": list(MODELS),
        "frozen_corpus": {"version": FROZEN_CORPUS_VERSION, "digest": FROZEN_CORPUS_DIGEST},
        "source_digests": task_digests,
        "counts": {
            "logical_cells": len(cells),
            "execution_slots": live_turns + sum(not item["model_dependent"] for item in cells),
            "model_dependent_cells": sum(item["model_dependent"] for item in cells),
            "shared_deterministic_cells": sum(not item["model_dependent"] for item in cells),
            "live_model_or_worker_turns": live_turns,
            "deterministic_observation_slots": sum(not item["model_dependent"] for item in cells),
            "by_study": by_study,
        },
        "s5_model_independence": {
            "decision": "model-independent",
            "reason": "S5 observes deterministic validator/mutation behavior and has no model response; labeling it per model would pseudo-replicate identical observations.",
            "old_manifest_cells": 270,
            "revised_cells": 135,
        },
        "cells": cells,
        "notes": [
            "This is a generated execution plan, not a result file.",
            "Protocol amendment 2026-09-14.ac3-model-family-amendment.1 replaces prospective GPT-6 Astra with Claude Sonnet 5 before comparative inference; historical Astra evidence is immutable.",
            "Logical-cell count preserves the old S3 definition; execution slots are 567 because each S3 logical cell has two live worker turns.",
            "S3 worker_turns=2 means one logical cell launches two concurrent isolated worker turns.",
            "S4 C4 and C5 use local deterministic implementations; no auxiliary model, embedding API, or memory service calls are permitted.",
            "No registered AC-3 benchmark cell has been executed.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate the WI022 AC-3 pre-inference execution manifest")
    parser.add_argument("--root", default=None)
    parser.add_argument("--out", default="evidence/runs/20260914-wi022-ac3-execution-manifest-v3.json")
    args = parser.parse_args(argv)
    output = Path(args.out)
    if not output.is_absolute():
        output = (Path(args.root) if args.root else Path.cwd()) / output
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_manifest(args.root), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"GENERATED {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
