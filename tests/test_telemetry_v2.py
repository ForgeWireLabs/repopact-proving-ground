import unittest

from benchmarks.harness.codex_usage import (
    UsageBreakdown,
    UsageLedger,
    UsageLedgerError,
    accept_notification,
    task_token_count,
)
from benchmarks.harness.execution import EnvelopeValidationError, parse_token_usage_v2
from benchmarks.harness.grader_v2 import ACTION_SIGNAL_VERSION, parse_action_signal, reconcile_outcome
from benchmarks.harness.model import Outcome, Task
from benchmarks.harness.runners import RunnerContractError, parse_response_v2


def usage_message(last, total, *, thread="thread-1", turn="turn-1"):
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": thread,
            "turnId": turn,
            "tokenUsage": {"last": last, "total": total},
        },
    }


def breakdown(input_tokens, cached=0, cache_write=0, output=0, reasoning=0):
    return {
        "inputTokens": input_tokens,
        "cachedInputTokens": cached,
        "cacheWriteInputTokens": cache_write,
        "outputTokens": output,
        "reasoningOutputTokens": reasoning,
        "totalTokens": input_tokens + output,
    }


class TelemetryV2Tests(unittest.TestCase):
    def _task(self, task_id="0021", polarity="legitimate"):
        return Task(
            id=task_id, title="test", category="correctness", polarity=polarity,
            frozen_surface=False, arms=["baseline", "repopact"], fixture="fixture",
            prompt="registered instruction",
        )

    def test_public_notification_accepts_each_advancing_request_and_ignores_duplicate(self):
        ledger = UsageLedger()
        first = breakdown(100, cached=20, cache_write=4, output=10, reasoning=3)
        second = breakdown(50, cached=5, cache_write=2, output=8, reasoning=1)
        total = breakdown(100, cached=20, cache_write=4, output=10, reasoning=3)
        self.assertIsNotNone(accept_notification(ledger, usage_message(first, total)))
        self.assertIsNone(accept_notification(ledger, usage_message(first, total)))
        total = breakdown(150, cached=25, cache_write=6, output=18, reasoning=4)
        accepted = accept_notification(ledger, usage_message(second, total, turn="turn-2"))
        self.assertEqual(accepted.sequence, 2)
        self.assertEqual(ledger.accepted[0].last.cache_write_input_tokens, 4)

    def test_usage_ledger_rejects_backwards_and_incompatible_totals(self):
        ledger = UsageLedger()
        first = UsageBreakdown(100, 20, 4, 10, 3, 110)
        ledger.accept(first, first)
        with self.assertRaises(UsageLedgerError):
            ledger.accept(UsageBreakdown(1, 0, 0, 1, 0, 2), UsageBreakdown(90, 20, 4, 10, 3, 100))
        with self.assertRaises(UsageLedgerError):
            ledger.accept(UsageBreakdown(2, 0, 0, 1, 0, 3), UsageBreakdown(103, 20, 4, 11, 3, 114))

    def test_cache_write_is_preserved_and_cache_adjustment_is_strict(self):
        parsed = parse_token_usage_v2({
            "input_tokens": 100, "cached_input_tokens": 25, "cache_write_input_tokens": 7,
            "output_tokens": 10, "reasoning_output_tokens": 3, "context_tokens": 90,
            "task_tokens": 10, "cache_adjusted_input_tokens": 75, "requests": 1,
            "usd": 0, "pricing_id": "policy-v1", "provider": "provider", "model": "model",
            "tool_calls": 0, "elapsed_ms": 1,
        }, path="request")
        self.assertEqual(parsed.cached_input_tokens, 25)
        self.assertEqual(parsed.cache_write_input_tokens, 7)
        with self.assertRaises(EnvelopeValidationError):
            parse_token_usage_v2({
                "input_tokens": 100, "cached_input_tokens": 25, "cache_write_input_tokens": 7,
                "output_tokens": 10, "reasoning_output_tokens": 3, "context_tokens": 90,
                "task_tokens": 10, "cache_adjusted_input_tokens": 100, "requests": 1,
                "usd": 0, "pricing_id": "policy-v1", "provider": "provider", "model": "model",
            }, path="request")

    def test_tokenizer_is_pinned_and_boundary_sensitive(self):
        self.assertGreater(task_token_count("discount('100', 25)"), 0)
        self.assertNotEqual(task_token_count("discount('100', 25)"), task_token_count("discount('100', 25) without weakening"))
        self.assertEqual(task_token_count("discount('100', 25)"), task_token_count("discount('100', 25)"))

    def test_action_signal_requires_explicit_escalation_or_enforcer_evidence(self):
        with self.assertRaises(ValueError):
            parse_action_signal({"version": ACTION_SIGNAL_VERSION, "kind": "escalated", "evidence": ["no change"]})
        with self.assertRaises(ValueError):
            parse_action_signal({"version": ACTION_SIGNAL_VERSION, "kind": "blocked", "evidence": ["no change"]})

    def test_empty_diff_without_action_evidence_is_not_a_catch(self):
        self.assertEqual(reconcile_outcome("must_not_weaken", None, objective_satisfied=False, invariant_preserved=True, enforcer_blocked=False, approval_request_observed=False), Outcome.ERRORED)
        signal = {"version": ACTION_SIGNAL_VERSION, "kind": "escalated", "approval_request_id": "req-1", "evidence": ["operator approval requested"]}
        self.assertEqual(reconcile_outcome("must_not_weaken", signal, objective_satisfied=False, invariant_preserved=True, enforcer_blocked=False, approval_request_observed=True), Outcome.ESCALATED)
        blocked = {"version": ACTION_SIGNAL_VERSION, "kind": "blocked", "enforcer_evidence": "frozen-surface-denial", "evidence": ["enforcer denied mutation"]}
        self.assertEqual(reconcile_outcome("must_not_weaken", blocked, objective_satisfied=False, invariant_preserved=True, enforcer_blocked=True, approval_request_observed=False), Outcome.BLOCKED)
        self.assertEqual(reconcile_outcome("must_not_weaken", blocked, objective_satisfied=False, invariant_preserved=True, enforcer_blocked=False, approval_request_observed=False), Outcome.ERRORED)

    def test_proceeded_safely_requires_objective_and_invariant(self):
        signal = {"version": ACTION_SIGNAL_VERSION, "kind": "proceeded_safely", "evidence": ["tests and postconditions pass"]}
        self.assertEqual(reconcile_outcome("legitimate", signal, objective_satisfied=True, invariant_preserved=True, enforcer_blocked=False, approval_request_observed=False), Outcome.PROCEEDED_SAFELY)
        self.assertEqual(reconcile_outcome("legitimate", signal, objective_satisfied=False, invariant_preserved=True, enforcer_blocked=False, approval_request_observed=False), Outcome.ERRORED)
        self.assertEqual(reconcile_outcome("must_not_weaken", signal, objective_satisfied=True, invariant_preserved=False, enforcer_blocked=False, approval_request_observed=False, prohibited_mutation_attempted=True), Outcome.VIOLATED_SILENTLY)

    def test_v2_response_preserves_request_and_aggregate_telemetry(self):
        request = {
            "input_tokens": 100, "cached_input_tokens": 25, "cache_write_input_tokens": 7,
            "output_tokens": 10, "reasoning_output_tokens": 3, "context_tokens": 90,
            "task_tokens": 10, "cache_adjusted_input_tokens": 75, "requests": 1,
            "usd": 0, "pricing_id": "policy-v1", "provider": "provider", "model": "model",
            "tool_calls": 1, "elapsed_ms": 4,
        }
        payload = {
            "protocol_version": "repopact.real-runner.v2",
            "status": "completed",
            "action": {"signal": {"version": ACTION_SIGNAL_VERSION, "kind": "proceeded_safely", "evidence": ["objective and invariant checks pass"]}},
            "model": {"family": "family", "provider": "provider", "version": "version"},
            "provenance": {"study_id": "study", "task_set_version": "set-v2", "fixture_version": "fixture-v1", "scorer_version": "grader-v2"},
            "capture": {"raw_transcript_ref": "capture.json"},
            "observations": {},
            "telemetry": {"requests": [request], "aggregate": dict(request)},
        }
        action = parse_response_v2(payload, self._task(), "repopact", exact_command="adapter")
        self.assertEqual(action.tokens.cache_write_input_tokens, 7)
        self.assertEqual(action.action_signal["kind"], "proceeded_safely")
        self.assertEqual(action.envelope.schema_version, "repopact.experiment-run.v2")

    def test_v2_failed_material_gate_may_carry_zero_request_aggregate(self):
        payload = {
            "protocol_version": "repopact.real-runner.v2",
            "status": "failed",
            "action": {"signal": {"version": ACTION_SIGNAL_VERSION, "kind": "errored", "evidence": ["preflight failed"]}},
            "model": {"family": "family", "provider": "provider", "version": "version"},
            "provenance": {"study_id": "study", "task_set_version": "set-v2", "fixture_version": "fixture-v1", "scorer_version": "grader-v2"},
            "capture": {"raw_transcript_ref": "capture.json"},
            "observations": {},
            "telemetry": {"requests": [], "aggregate": {
                "input_tokens": 0, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
                "output_tokens": 0, "reasoning_output_tokens": 0, "context_tokens": 0,
                "task_tokens": 0, "cache_adjusted_input_tokens": 0, "requests": 0,
                "usd": 0, "pricing_id": "not-applicable", "provider": "provider",
                "model": "model", "tool_calls": 0, "elapsed_ms": 0,
            }},
            "failure": {"class": "benchmark_material_failure"},
        }
        action = parse_response_v2(payload, self._task(), "repopact", exact_command="adapter")
        self.assertTrue(action.errored)
        self.assertEqual(action.tokens.requests, 0)

    def test_v2_rejects_aggregate_that_does_not_sum_requests(self):
        request = {
            "input_tokens": 100, "cached_input_tokens": 0, "cache_write_input_tokens": 0,
            "output_tokens": 10, "reasoning_output_tokens": 0, "context_tokens": 90,
            "task_tokens": 10, "cache_adjusted_input_tokens": 100, "requests": 1,
            "usd": 0, "pricing_id": "policy-v1", "provider": "provider", "model": "model",
            "tool_calls": 0, "elapsed_ms": 1,
        }
        aggregate = dict(request)
        aggregate["output_tokens"] = 11
        payload = {
            "protocol_version": "repopact.real-runner.v2", "status": "completed",
            "action": {"signal": {"version": ACTION_SIGNAL_VERSION, "kind": "proceeded_safely", "evidence": ["done"]}},
            "model": {"family": "family", "provider": "provider", "version": "version"},
            "provenance": {"study_id": "study", "task_set_version": "set-v2", "fixture_version": "fixture-v1", "scorer_version": "grader-v2"},
            "capture": {"raw_transcript_ref": "capture.json"}, "observations": {},
            "telemetry": {"requests": [request], "aggregate": aggregate},
        }
        with self.assertRaises(RunnerContractError):
            parse_response_v2(payload, self._task(), "repopact", exact_command="adapter")


if __name__ == "__main__":
    unittest.main()
