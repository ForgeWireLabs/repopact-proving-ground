"""Run exactly one disposable, non-benchmark admission probe for one model family."""
from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path

from .empirical import EMPIRICAL_EXECUTOR_VERSION, EmpiricalExecutor
from .empirical_workspace import EmpiricalWorkspace
from .execution import ModelIdentity
from .workspace_io import WorkspaceIOError, read_bytes_after_quiescence


ADMISSION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["probe_version", "status", "evidence"],
    "properties": {
        "probe_version": {"type": "string", "const": "wi022-ac3-admission.v1"},
        "status": {"type": "string", "enum": ["pass", "fail"]},
        "evidence": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
}


FAMILIES = {
    "gpt-5.6": ModelIdentity("gpt-5.6", "openai", "gpt-5.6-luna"),
    "gpt-6": ModelIdentity("gpt-6", "openai", "gpt-6-astra"),
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_probe(family: str, *, capture_root: str | Path) -> dict[str, object]:
    if family not in FAMILIES:
        raise ValueError(f"unsupported admission family: {family}")
    identity = FAMILIES[family]
    repo_root = Path(__file__).resolve().parents[2]
    with EmpiricalWorkspace.allocate(repo_root=repo_root, prefix=f"wi022-admission-{family.replace('.', '-')}") as allocated:
        workspace = allocated.path
        fixture = workspace / "admission-fixture.txt"
        fixture.write_text("WI022 disposable admission fixture\n", encoding="utf-8")
        fixture_digest = _digest(fixture)
        prompt = (
            "This is a disposable runtime admission probe, not a registered benchmark task. "
            "Use your available file/command tools in the current workspace: read "
            "admission-fixture.txt, create admission-result.txt containing exactly "
            "'probe tool operation complete', then return only JSON matching the supplied schema "
            "with probe_version 'wi022-ac3-admission.v1', status 'pass', and concise evidence."
        )
        executor = EmpiricalExecutor(
            model=identity,
            cwd=workspace,
            capture_root=capture_root,
            pricing_id="chatgpt-subscription-unmetered-2026-09-14",
            output_schema=ADMISSION_SCHEMA,
            workspace_root=allocated.configured_root,
        )
        turn = executor.run(
            prompt,
            capture_name=f"20260914-wi022-ac3-admission/{family}.json",
            study_id="WI022-AC3-admission",
            case_id=f"probe-{family}",
            condition="admission",
            fixture="disposable-admission-fixture",
            fixture_version="wi022-admission-fixture.v1",
            repetition=0,
            seed=0,
            workspace_identity={"fixture": "admission-fixture.txt", "fixture_sha256": fixture_digest},
            auxiliary_calls=(),
        )
        result_file = workspace / "admission-result.txt"
        turn_completed_monotonic = time.monotonic()
        if turn.turn_completed_elapsed_ms is not None:
            turn_completed_monotonic -= max(0.0, (turn.elapsed_ms - turn.turn_completed_elapsed_ms) / 1000.0)
        try:
            read_result = read_bytes_after_quiescence(
                result_file,
                workspace=workspace,
                turn_completed_monotonic=turn_completed_monotonic,
            )
            tool_postcondition = read_result.content == b"probe tool operation complete"
            read_evidence = read_result.evidence
        except WorkspaceIOError as exc:
            tool_postcondition = False
            read_evidence = exc.evidence
        envelope = turn.to_envelope(
            study_id="WI022-AC3-admission",
            case_id=f"probe-{family}",
            condition="admission",
            fixture="disposable-admission-fixture",
            fixture_version="wi022-admission-fixture.v1",
            repetition=0,
            seed=0,
            scorer_version="wi022-ac3-admission.v1",
            success=tool_postcondition and turn.final_output.get("status") == "pass",
            observations={"tool_postcondition": tool_postcondition, "model_report": turn.final_output, "workspace_read": read_evidence},
        )
        return {
            "family": family,
            "model": asdict(identity),
            "probe_version": "wi022-ac3-admission.v1",
            "executor_version": EMPIRICAL_EXECUTOR_VERSION,
            "result": "PASS" if tool_postcondition and turn.final_output.get("status") == "pass" else "FAIL",
            "tool_postcondition": tool_postcondition,
            "workspace_read": read_evidence,
            "structured_output": turn.final_output,
            "thread_id": turn.thread_id,
            "turn_id": turn.turn_id,
            "requests": turn.aggregate.requests,
            "aggregate": asdict(turn.aggregate),
            "tool_calls": turn.tool_calls,
            "capture_ref": turn.capture_ref,
            "capture_digest": turn.capture_digest,
            "schema_digest": turn.schema_identity["schema_digest"],
            "runtime_identity": turn.runtime_identity,
            "envelope_validation": "PASS",
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one non-benchmark WI022 AC-3 admission probe")
    parser.add_argument("--family", choices=sorted(FAMILIES), required=True)
    parser.add_argument("--capture-root", default="evidence/captures")
    args = parser.parse_args(argv)
    report = run_probe(args.family, capture_root=args.capture_root)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
