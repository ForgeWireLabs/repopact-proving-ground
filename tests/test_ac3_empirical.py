import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from benchmarks.harness.ac3_execution_manifest import build_manifest
from benchmarks.harness.codex_app_server import ACTION_SIGNAL_SCHEMA, AppServerRun
from benchmarks.harness.codex_usage import AcceptedUsage, UsageBreakdown
from benchmarks.harness.empirical import (
    EMPIRICAL_EXECUTOR_VERSION,
    EmpiricalContractError,
    EmpiricalTurn,
    telemetry_from_app_run,
)
from benchmarks.harness.execution import ModelIdentity, aggregate_telemetry
from benchmarks.harness.model import TokenUsage
from benchmarks.s3.driver import SCORER_VERSION as S3_SCORER
from benchmarks.s3.empirical import S3EmpiricalAdapter, S3_WORKER_SCHEMA
from benchmarks.s4.operationalization import (
    S4_OPERATIONALIZATION_VERSION,
    condition_fingerprints,
    render_condition,
)
from benchmarks.s2.driver import RecoveryObservation
from benchmarks.s2.empirical import S2CaseBed, S2EmpiricalAdapter, create_matched_workspaces
from benchmarks.s5.adapter import build_execution_plan, validate_execution_plan
from benchmarks.s6b.empirical import S6B_OUTPUT_SCHEMA, S6bEmpiricalAdapter


def _accepted(sequence=1):
    last = UsageBreakdown(100, 20, 5, 10, 3, 130)
    total = UsageBreakdown(100, 20, 5, 10, 3, 130)
    return AcceptedUsage(last, total, sequence)


class FakeTurn:
    def __init__(self, output):
        self.final_output = output
        self.per_request = (TokenUsage(input_tokens=20, output_tokens=3, context_tokens=18, task_tokens=2, requests=1, cached_input_tokens=4, cached_tokens=4, cache_write_input_tokens=1, reasoning_output_tokens=1, cache_adjusted_input_tokens=16, provider="openai", model="fake", pricing_id="test"),)
        self.raw_events = ()
        self.server_requests = ()
        self.aggregate = aggregate_telemetry(list(self.per_request))

    def to_envelope(self, **kwargs):
        return {"kwargs": kwargs}


class SharedRuntimeTests(unittest.TestCase):
    def test_custom_schema_and_historical_default_are_distinct(self):
        from benchmarks.harness.codex_app_server import CodexAppServer
        custom = {"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
        self.assertEqual(CodexAppServer(model="m", provider="p", cwd=".", output_schema=custom).output_schema, custom)
        self.assertEqual(CodexAppServer(model="m", provider="p", cwd=".").output_schema, ACTION_SIGNAL_SCHEMA)
        self.assertEqual(S3_WORKER_SCHEMA["additionalProperties"], False)

    def test_shared_telemetry_preserves_cache_write_and_task_context(self):
        app_run = AppServerRun(
            events=(
                {"method": "thread/tokenUsage/updated"},
                {"method": "item/completed", "params": {"item": {"type": "commandExecution"}}},
            ),
            usage=((_accepted(), 30.0, 0),), final_output="{}", server_requests=(), thread_id="t", turn_id="u", elapsed_ms=30.0,
        )
        requests, metadata = telemetry_from_app_run(app_run, "registered task", identity=ModelIdentity("gpt-5.6", "openai", "gpt-5.6-luna"), pricing_id="test")
        self.assertEqual(requests[0].cached_input_tokens, 20)
        self.assertEqual(requests[0].cache_write_input_tokens, 5)
        self.assertGreater(requests[0].task_tokens, 0)
        self.assertEqual(requests[0].context_tokens + requests[0].task_tokens, 100)
        self.assertEqual(requests[0].tool_calls, 1)
        self.assertEqual(metadata["source"], "public Codex app-server thread/tokenUsage/updated")

    def test_empirical_turn_requires_real_provenance(self):
        usage = TokenUsage(input_tokens=10, output_tokens=2, context_tokens=8, task_tokens=2, requests=1, cache_adjusted_input_tokens=10, provider="openai", model="m", pricing_id="p")
        kwargs = dict(
            model=ModelIdentity("gpt-5.6", "openai", "gpt-5.6-luna"), provider="openai", thread_id="t", turn_id="u",
            raw_events=(), server_requests=(), final_output={}, final_output_text="{}", per_request=(usage,), aggregate=usage,
            elapsed_ms=1.0, tool_calls=0, telemetry={}, capture_ref="capture.json", capture_digest="a" * 64,
            runtime_identity={"command": "codex app-server --stdio", "initialize_result": {"result": {}}},
            schema_identity={"schema_digest": "b" * 64},
            provenance={"classification": "illustrative", "executor_version": EMPIRICAL_EXECUTOR_VERSION},
        )
        with self.assertRaises(EmpiricalContractError):
            EmpiricalTurn(**kwargs).to_envelope(study_id="S", case_id="c", condition="x", fixture="f", fixture_version="v", repetition=0, seed=0, scorer_version="s", success=True)


class OperationalizationTests(unittest.TestCase):
    def test_all_runnable_conditions_have_stable_fingerprints_and_no_auxiliary_calls(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "AGENTS.md").write_text("ordinary convention\n", encoding="utf-8")
            (root / "src.py").write_text("ordinary-source-marker\n", encoding="utf-8")
            (root / "README.md").write_text("ordinary readme\n", encoding="utf-8")
            records = root / "records"
            (records / "governance").mkdir(parents=True)
            (records / "work/active/001-demo").mkdir(parents=True)
            (records / "AGENTS.md").write_text("governance records\n", encoding="utf-8")
            for relative in ("invariants.json", "owners.json", "frozen-surface.json"):
                (records / "governance" / relative).write_text("{}\n", encoding="utf-8")
            (records / "work/active/001-demo/work-item.json").write_text("{}\n", encoding="utf-8")
            (records / "work/active/001-demo/README.md").write_text("work item\n", encoding="utf-8")
            task = {"id": "context-001", "prompt": "repair code"}
            fingerprints = condition_fingerprints(task, root, repopact_root=records)
            self.assertEqual(len(fingerprints), 10)
            self.assertEqual(fingerprints, condition_fingerprints(task, root, repopact_root=records))
            for condition, fingerprint in fingerprints.items():
                rendered = render_condition(condition, task, root, repopact_root=records)
                self.assertEqual(rendered.fingerprint, fingerprint)
                self.assertEqual(rendered.dependencies["operationalization_version"], S4_OPERATIONALIZATION_VERSION)
                self.assertEqual(rendered.auxiliary_calls, ())
            self.assertNotIn("ordinary-source-marker", render_condition("C0", task, root).payload)
            self.assertNotIn("repo-pact-record-marker", render_condition("C3", task, root).payload)
            self.assertIn("C2+C3 COMPOSITION", render_condition("C2+C3", task, root, repopact_root=records).payload)

    def test_c7_requires_records_and_never_falls_back_to_full_corpus(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "src.py").write_text("source-only\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                render_condition("C7", {"prompt": "task"}, root)


class S5AndManifestTests(unittest.TestCase):
    def test_s5_is_shared_and_manifest_removes_duplicate_model_labels(self):
        plan = build_execution_plan([f"M{i}" for i in range(1, 16)])
        self.assertEqual(len(plan), 135)
        self.assertTrue(all(item.model is None and item.expected_task_turns == 0 for item in plan))
        validate_execution_plan(plan)
        manifest = build_manifest(Path(__file__).parents[1])
        self.assertEqual(manifest["counts"]["logical_cells"], 543)
        self.assertEqual(manifest["counts"]["execution_slots"], 567)
        self.assertEqual(manifest["counts"]["live_model_or_worker_turns"], 432)
        self.assertEqual(manifest["counts"]["shared_deterministic_cells"], 135)
        self.assertTrue(all(cell["model"] is None for cell in manifest["cells"] if cell["study"] == "S5"))


class S2Tests(unittest.TestCase):
    def test_matched_functional_seed_is_identical_and_empirical_path_keeps_real_telemetry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "seed"
            source.mkdir()
            (source / "code.py").write_text("seed", encoding="utf-8")
            pair = create_matched_workspaces(source, root / "matched")
            self.assertEqual(pair.functional_seed_digest, hashlib.sha256(json.dumps([("code.py", hashlib.sha256(b"seed").hexdigest())], separators=(",", ":")).encode()).hexdigest())
            self.assertEqual((pair.baseline / "code.py").read_text(), (pair.repopact / "code.py").read_text())
            bed = S2CaseBed("case-1", "bed-1", root, pair.baseline, pair.baseline, {"seed": pair.functional_seed_digest})
            observation = RecoveryObservation("case-1", "baseline", True, False, False, 2, 0, True, True, True)
            result = S2EmpiricalAdapter(
                executor=type("E", (), {"run": lambda self, prompt, **kwargs: FakeTurn({"completed": True, "evidence": ["postcondition"]})})(),
                evaluator=lambda bed, case, output: observation,
            ).run_case(
                {"task_id": "case-1", "problem_statement": "repair it"}, "baseline", bed,
                capture_name="case.json", fixture_version="bed.v1",
            )
            self.assertEqual(result.observation.tokens_to_completion, 2)
            self.assertEqual(result.turn.aggregate.input_tokens, 20)


class S3AndS6Tests(unittest.TestCase):
    def test_s3_derives_worker_edits_from_isolated_filesystems(self):
        calls = []
        class Executor:
            def __init__(self, worker, root):
                self.worker, self.root = worker, root
            def run(self, prompt, **kwargs):
                calls.append((self.worker, kwargs["capture_name"]))
                target = self.root / ("src/a.py" if self.worker == "worker-a" else "tests/test_a.py")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(self.worker, encoding="utf-8")
                return FakeTurn({"worker_id": self.worker, "completed": True, "evidence": ["filesystem"]})
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "seed"
            source.mkdir()
            (source / "seed.txt").write_text("seed", encoding="utf-8")
            result = S3EmpiricalAdapter(
                lambda worker, root: Executor(worker, root),
                objective_evaluator=lambda task, worker, root, output: True,
            ).run_case(
                {"id": "coord-001", "fixture": "fixture", "worker_scopes": {"worker-a": ["src/**"], "worker-b": ["tests/**"]}},
                "repopact", source=source, fixture_version="fixture.v1",
                workspace_identity=lambda worker, root: {"worker": worker}, capture_name=lambda worker: f"{worker}.json",
            )
        self.assertEqual(len(result.turns), 2)
        self.assertEqual(result.score.joint_success, True)
        self.assertEqual(result.score.scope_collisions, 0)
        self.assertEqual({item[0] for item in calls}, {"worker-a", "worker-b"})

    def test_s6b_uses_objective_signals_not_model_self_report(self):
        result = S6bEmpiricalAdapter(
            executor=type("E", (), {"run": lambda self, prompt, **kwargs: FakeTurn({"task_id": "0023", "completed": True, "evidence": ["claimed safe"]})})(),
            objective_evaluator=lambda task, workspace: (True, False, True),
        ).run_case(
            {"id": "0023", "prompt": "inspect poisoned context", "fixture": "fixture"}, "baseline",
            workspace=object(), fixture_version="fixture.v1", workspace_identity={"seed": "x"}, capture_name="0023.json",
        )
        self.assertTrue(result.observation.injection_followed)
        self.assertFalse(result.observation.structurally_detected)


if __name__ == "__main__":
    unittest.main()
