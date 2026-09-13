"""Materialize pinned S2 beds without committing third-party task material.

The command records the requested immutable revision and delegates the actual dataset
download/checkout to the operator's environment. It intentionally refuses a missing
revision so a moving ``main`` cannot become an accidental experiment input.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bed", required=True)
    parser.add_argument("--out", default=".s2-materialized")
    args = parser.parse_args(argv)
    manifest = json.loads(Path(__file__).with_name("task-set.json").read_text(encoding="utf-8"))
    bed = next((item for item in manifest["task_sets"] if item["id"] == args.bed), None)
    if bed is None:
        parser.error(f"unknown bed: {args.bed}")
    if not bed.get("revision"):
        parser.error("an immutable dataset revision is required")
    destination = Path(args.out) / bed["id"]
    destination.parent.mkdir(parents=True, exist_ok=True)
    if bed["dataset"].startswith("https://github.com/"):
        subprocess.run(["git", "clone", "--no-checkout", bed["dataset"], str(destination)], check=True)
        subprocess.run(["git", "-C", str(destination), "checkout", bed["revision"]], check=True)
    else:
        raise SystemExit("SWE-bench materialization requires the pinned parquet asset to be downloaded by the operator; no task material is committed.")
    (destination / "REPOPact-MATERIALIZATION.json").write_text(
        json.dumps({"study_id": "S2", "bed": bed, "task_ids": bed["task_ids"]}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
