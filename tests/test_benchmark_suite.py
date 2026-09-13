import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.harness.capture import CaptureIntegrityError, load_capture, write_capture
from benchmarks.harness.execution import (
    ModelIdentity,
    RunEnvelope,
    aggregate_telemetry,
    validate_envelope,
)
from benchmarks.harness.model import Task, TokenUsage
from benchmarks.harness.registry import RegistrationError
from benchmarks.harness.runners import (
    REAL_RUNNER_CONTRACT_VERSION,
    RunnerContractError,
    build_request,
    parse_response,
)
from benchmarks.s2.driver import RecoveryObservation, load_task_set as load_s2, score_recovery
from benchmarks.s3.driver import (
    CoordinationEvent,
    WorkerResult,
    load_task_set as load_s3,
    score_coordination,
)
from benchmarks.s4.driver import (
    ContextObservation,
    ParetoPoint,
    analyze,
    aggregate_observation,
    load_conditions,
    pareto_frontier,
    scaling_curve,
    validate_condition,
)
from benchmarks.s5.adapter import adapt_result, summarize as summarize_drift
from benchmarks.s6b.driver import load_registered_tasks, score as score_injection
from benchmarks.s6b.driver import InjectionObservation


def usage(input_tokens=100, *, context=60, task=40, usd=0.01, provider="provider", model="model-v1", pricing="price-2026-09-13"):
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=20,
        context_tokens=context,
        task_tokens=task,
        requests=1,
        usd=usd,
        cached_tokens=5,
        cache_adjusted_input_tokens=input_tokens - 5,
        provider=provider,
        model=model,
        pricing_id=pricing,
        elapsed_ms=10.0,
    )


class BenchmarkSuiteTests(unittest.TestCase):
    def test_common_envelope_validation_and_illustrative_classification(self):
        request = usage()
        envelope = RunEnvelope(
            schema_version="repopact.experiment-run.v1", study_id="S4", case_id="context-001",
            condition="C7", fixture="fixture", fixture_version="fixture.v1", repetition=0, seed=7,
            model=ModelIdentity("mock-family", "mock", "mock-v1"), temperature_policy="deterministic",
            scorer_version="s4-token-economy.v1", started_at=None, ended_at=None, elapsed_ms=10.0,
            completed=True, success=True, failure_class=None, per_request=[request],
            aggregate=aggregate_telemetry([request]), observations={"success": True},
            provenance={"classification": "illustrative"}, illustrative=True,
        )
        self.assertEqual(validate_envelope(envelope), [])
        envelope.aggregate = TokenUsage()
        self.assertTrue(any("aggregate telemetry" in problem for problem in validate_envelope(envelope)))

    def test_empirical_envelope_requires_capture_and_exact_command(self):
        request = usage()
        envelope = RunEnvelope(
            schema_version="repopact.experiment-run.v1", study_id="S1", case_id="0001",
            condition="baseline", fixture="fixture", fixture_version="fixture.v1", repetition=0, seed=0,
            model=ModelIdentity("family", "provider", "version"), temperature_policy="0",
            scorer_version="grader.v1", started_at=None, ended_at=None, elapsed_ms=1,
            completed=True, success=True, failure_class=None, per_request=[request], aggregate=request,
        )
        errors = validate_envelope(envelope, empirical=True)
        self.assertIn("empirical runs require raw_capture_ref", errors)
        self.assertIn("empirical runs require exact_command", errors)

    def test_pactbench_loader_remains_backward_compatible(self):
        from benchmarks.harness.run import load_tasks
        tasks = load_tasks("benchmarks/pactbench/tasks")
        self.assertEqual(len(tasks), 24)
        self.assertEqual(tasks[-1].id, "0024")
        self.assertTrue(tasks[-1].prompt)

    def test_real_runner_rejects_incomplete_required_telemetry(self):
        task = Task("0001", "test", "correctness", "must_not_weaken", False, ["baseline", "repopact"])
        response = {
            "protocol_version": REAL_RUNNER_CONTRACT_VERSION,
            "status": "completed",
            "action": {name: False for name in ("weakened_guarantee", "escalated", "blocked_by_enforcer", "completed_task", "errored")},
            "model": {"family": "f", "provider": "p", "version": "v"},
            "provenance": {"task_set_version": "tasks.v1", "fixture_version": "fixture.v1"},
            "capture": {"raw_transcript_ref": "S1/baseline/0001.json"},
            "observations": {},
            "telemetry": {"requests": [{"output_tokens": 1, "context_tokens": 1, "task_tokens": 1, "usd": 0.01}], "aggregate": {}},
        }
        with self.assertRaises(RunnerContractError):
            parse_response(response, task, "baseline", exact_command="agent-wrapper")

    def test_real_runner_request_carries_study_spec(self):
        task = Task("0001", "test", "correctness", "must_not_weaken", False, ["baseline"], prompt="continue")
        request = build_request(task, "baseline", {"study_id": "S2", "seed": 5})
        self.assertEqual(request["protocol_version"], REAL_RUNNER_CONTRACT_VERSION)
        self.assertEqual(request["study_spec"]["study_id"], "S2")
        self.assertEqual(request["task"]["prompt"], "continue")

    def test_real_runner_parses_complete_versioned_response_into_envelope(self):
        task = Task("0001", "test", "correctness", "must_not_weaken", False, ["baseline"], prompt="continue")
        request = usage()
        telemetry = {
            "input_tokens": request.input_tokens,
            "output_tokens": request.output_tokens,
            "context_tokens": request.context_tokens,
            "task_tokens": request.task_tokens,
            "requests": 1,
            "usd": request.usd,
            "cached_tokens": request.cached_tokens,
            "cache_adjusted_input_tokens": request.cache_adjusted_input_tokens,
            "pricing_id": request.pricing_id,
            "provider": request.provider,
            "model": request.model,
            "elapsed_ms": request.elapsed_ms,
        }
        response = {
            "protocol_version": REAL_RUNNER_CONTRACT_VERSION,
            "status": "completed",
            "action": {"weakened_guarantee": False, "escalated": True, "blocked_by_enforcer": False, "completed_task": False, "errored": False},
            "model": {"family": "family", "provider": "provider", "version": "version"},
            "provenance": {"study_id": "S1", "task_set_version": "tasks.v1", "fixture_version": "fixture.v1", "scorer_version": "grader.v1"},
            "capture": {"raw_transcript_ref": "S1/baseline/0001.json"},
            "observations": {"postcondition": "guard-preserved"},
            "telemetry": {"requests": [telemetry], "aggregate": telemetry},
        }
        action = parse_response(response, task, "baseline", exact_command="agent-wrapper")
        self.assertTrue(action.escalated)
        self.assertFalse(validate_envelope(action.envelope, empirical=True))

    def test_s2_manifest_and_three_part_recovery_rubric(self):
        registered = load_s2()
        self.assertEqual([record["id"] for record in registered.records], ["swe-bench-verified", "swe-evo"])
        observation = RecoveryObservation("case", "swe-evo", True, False, False, 100, 1, True, False, True)
        scored = score_recovery(observation)
        self.assertEqual(scored["state_recovery_score"], 0.667)
        self.assertNotIn("governance_continuity", scored)

    def test_s3_scores_conflict_duplicate_and_scope_collision(self):
        task = load_s3().records[0]
        results = [
            WorkerResult("worker-a", True, (CoordinationEvent("worker-a", "edit", "src/a.py", "a", "implement"),)),
            WorkerResult("worker-b", False, (
                CoordinationEvent("worker-b", "edit", "src/a.py", "b", "implement"),
                CoordinationEvent("worker-b", "edit", "src/a.py", "b", "implement"),
            )),
        ]
        score = score_coordination(task, results)
        self.assertEqual(score.conflicting_edits, 1)
        self.assertEqual(score.duplicated_work, 1)
        self.assertEqual(score.scope_collisions, 2)
        self.assertFalse(score.joint_success)

    def test_s4_condition_registry_and_failed_cost(self):
        self.assertEqual(set(load_conditions()), {"C0", "C1", "C2", "C2+C3", "C3", "C4", "C5", "C6", "C7", "C8", "C9"})
        validate_condition("C2+C3")
        with self.assertRaises(ValueError):
            validate_condition("C9")
        row = aggregate_observation(ContextObservation("context-001", "C7", (usage(),), False, 10))
        self.assertIsNone(row["usd_per_resolved_task"])
        self.assertIsNone(row["tokens_to_completion"])

    def test_s4_analysis_excludes_cheap_failed_point_and_builds_scaling_curve(self):
        good = ParetoPoint("C7", 0.10, 1.0)
        cheap_failed = ParetoPoint("C0", 0.001, 0.0)
        self.assertEqual([point.condition for point in pareto_frontier([good, cheap_failed])], ["C7"])
        observations = [
            ContextObservation("a", "C7", (usage(context=20, task=80),), True, 1),
            ContextObservation("b", "C7", (usage(context=40, task=60), usage(context=60, task=40)), True, 2),
        ]
        self.assertEqual(scaling_curve(observations)[1]["mean_context_tokens_per_request"], 50.0)
        self.assertTrue(analyze(observations)["illustrative"])

    def test_s5_adapter_preserves_blind_spot_and_metrics(self):
        rows = [
            adapt_result({"id": "M8", "repopact_detected": True, "repopact_latency": 1, "blind_spot": False}),
            adapt_result({"id": "M7", "repopact_detected": False, "repopact_latency": "inf", "blind_spot": True}),
        ]
        summary = summarize_drift(rows)
        self.assertEqual(summary["detection_rate"], 0.5)
        self.assertEqual(summary["blind_spots"], ["M7"])

    def test_s6b_uses_frozen_injection_seed_tasks(self):
        self.assertEqual([task["id"] for task in load_registered_tasks()], ["0023", "0024"])
        summary = score_injection([
            InjectionObservation("0023", "baseline", True, False),
            InjectionObservation("0023", "repopact", False, True),
        ])
        self.assertEqual(summary["injection_followed_rate"], 0.5)
        self.assertEqual(summary["structural_detection_rate"], 0.5)

    def test_capture_is_classified_and_rejects_secret_like_data(self):
        request = usage()
        envelope = RunEnvelope(
            schema_version="repopact.experiment-run.v1", study_id="S4", case_id="context-001",
            condition="C7", fixture="fixture", fixture_version="fixture.v1", repetition=0, seed=7,
            model=ModelIdentity("mock-family", "mock", "mock-v1"), temperature_policy="deterministic",
            scorer_version="s4-token-economy.v1", started_at=None, ended_at=None, elapsed_ms=10.0,
            completed=True, success=True, failure_class=None, per_request=[request], aggregate=request,
            provenance={"classification": "illustrative"}, illustrative=True,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = write_capture(tmp, envelope, raw_output={"note": "fixture"})
            data = load_capture(path)
            self.assertEqual(data["classification"], "illustrative")
        with self.assertRaises(CaptureIntegrityError):
            from benchmarks.harness.capture import assert_no_secrets
            assert_no_secrets({"api_key": "not-a-real-key"})


if __name__ == "__main__":
    unittest.main()
