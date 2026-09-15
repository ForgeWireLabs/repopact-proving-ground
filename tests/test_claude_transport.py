import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.harness.execution import ModelIdentity
from benchmarks.harness.empirical import EmpiricalExecutor
from benchmarks.harness.model import TokenUsage
from benchmarks.harness.transport import TransportRun
from benchmarks.harness.transport import ClaudeCodeTransport, EmpiricalTransportError


IDENTITY = ModelIdentity("claude-sonnet-5", "anthropic", "claude-sonnet-5")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}


class FakeProcess:
    def __init__(self, lines, *, returncode=0, timeout=False):
        self.pid = 4321
        self.returncode = returncode
        self._lines = lines
        self._timeout = timeout
        self.killed = False
        self.communicated = None

    def communicate(self, input=None, timeout=None):
        self.communicated = input
        if self._timeout and not self.killed:
            raise subprocess.TimeoutExpired("claude", timeout)
        return "\n".join(self._lines) + ("\n" if self._lines else ""), "fake stderr" if self.returncode else ""

    def kill(self):
        self.killed = True

    def poll(self):
        return self.returncode


def stream(*, model="claude-sonnet-5", usage=True, result=True, total_cost=0.123):
    events = [{"type": "system", "subtype": "init", "session_id": "sess-1", "model": model, "claude_code_version": "2.1.272", "tools": ["Read", "Write", "Edit", "Bash"]}]
    if usage:
        events.append({
            "type": "assistant",
            "message": {
                "model": model,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 10,
                    "cache_creation_input_tokens": 5,
                },
                "content": [{"type": "tool_use", "id": "tool-1", "name": "Write", "input": {}}],
            },
        })
    if result:
        events.append({"type": "result", "subtype": "success", "session_id": "sess-1", "model": model, "result": '{"ok": true}', "total_cost_usd": total_cost, "num_turns": 1})
    return [json.dumps(event) for event in events]


class ClaudeTransportTests(unittest.TestCase):
    def transport(self):
        return ClaudeCodeTransport(model=IDENTITY, cwd="C:/workspace", pricing_id="claude-client-estimate", timeout_seconds=30)

    def test_success_normalizes_usage_tools_identity_and_lifecycle(self):
        process = FakeProcess(stream())
        with patch("benchmarks.harness.transport.subprocess.Popen", return_value=process) as popen:
            run = self.transport().run("read the fixture", output_schema=SCHEMA)
        popen.assert_called_once()
        self.assertEqual(process.communicated, "read the fixture")
        command = popen.call_args.args[0]
        self.assertEqual(command[command.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(command[command.index("--permission-prompts") + 1], "none")
        self.assertEqual(command[command.index("--tools") + 1], "Read,Write,Edit,Bash")
        self.assertEqual(json.loads(command[command.index("--json-schema") + 1]), SCHEMA)
        self.assertEqual(run.model, IDENTITY)
        self.assertEqual(run.thread_id, "sess-1")
        self.assertEqual(run.per_request[0].input_tokens, 115)
        self.assertEqual(run.per_request[0].cached_input_tokens, 10)
        self.assertEqual(run.per_request[0].cache_write_input_tokens, 5)
        self.assertEqual(run.per_request[0].cache_adjusted_input_tokens, 100)
        self.assertIsNone(run.per_request[0].reasoning_output_tokens)
        self.assertEqual(run.per_request[0].tool_calls, 1)
        self.assertEqual(run.per_request[0].usd, 0.123)
        self.assertEqual(run.telemetry["cost_status"], "client-estimate")
        self.assertIn("reasoning_output_tokens", run.telemetry["unavailable_fields"])
        self.assertEqual(run.runtime_identity["permission_mode"], "acceptEdits")
        self.assertTrue(run.runtime_identity["process_lifecycle"]["process_terminated"])
        self.assertEqual(run.schema_version, "repopact.experiment-run.v3")

    def test_missing_request_usage_rejects_aggregate_only_surface(self):
        process = FakeProcess(stream(usage=False))
        with patch("benchmarks.harness.transport.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(EmpiricalTransportError, "per-request telemetry"):
                self.transport().run("read the fixture", output_schema=SCHEMA)

    def test_missing_exact_model_identity_rejects(self):
        lines = [json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1"}), json.dumps({"type": "result", "subtype": "success", "result": '{"ok": true}'})]
        process = FakeProcess(lines)
        with patch("benchmarks.harness.transport.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(EmpiricalTransportError, "exact model identity"):
                self.transport().run("read", output_schema=SCHEMA)

    def test_wrong_model_identity_rejects(self):
        process = FakeProcess(stream(model="claude-opus-5"))
        with patch("benchmarks.harness.transport.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(EmpiricalTransportError, "expected 'claude-sonnet-5'"):
                self.transport().run("read", output_schema=SCHEMA)

    def test_malformed_stream_and_nonzero_exit_reject(self):
        process = FakeProcess(["not-json"])
        with patch("benchmarks.harness.transport.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(EmpiricalTransportError, "non-JSON"):
                self.transport().run("read", output_schema=SCHEMA)
        process = FakeProcess(stream(), returncode=2)
        with patch("benchmarks.harness.transport.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(EmpiricalTransportError, "exited nonzero"):
                self.transport().run("read", output_schema=SCHEMA)

    def test_timeout_kills_owned_process(self):
        process = FakeProcess([], timeout=True)
        with patch("benchmarks.harness.transport.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(EmpiricalTransportError, "timed out"):
                self.transport().run("read", output_schema=SCHEMA)
        self.assertTrue(process.killed)

    def test_environment_selects_installed_executable(self):
        with patch.dict(os.environ, {"REPOPACT_CLAUDE_CODE_BIN": "C:/installed/claude.exe"}):
            self.assertEqual(self.transport()._command(SCHEMA)[0], "C:/installed/claude.exe")

    def test_shared_executor_normalizes_provider_specific_unavailable_fields(self):
        usage = TokenUsage(
            input_tokens=10, output_tokens=2, context_tokens=8, task_tokens=2,
            requests=1, usd=None, cached_input_tokens=0, cached_tokens=0,
            cache_write_input_tokens=0, reasoning_output_tokens=None,
            cache_adjusted_input_tokens=10, pricing_id="not-exposed",
            provider="anthropic", model="claude-sonnet-5", tool_calls=0,
        )

        class FakeTransport:
            def run(self, prompt, *, output_schema):
                return TransportRun(
                    model=IDENTITY, events=(), final_output='{"ok": true}',
                    server_requests=(), thread_id="sess", turn_id="turn", elapsed_ms=2.0,
                    per_request=(usage,), telemetry={"unavailable_fields": ["reasoning_output_tokens", "cost_usd"]},
                    runtime_identity={
                        "transport": "fake-claude", "command": "claude -p", "initialize_result": {"model": "claude-sonnet-5"},
                        "process_lifecycle": {"process_terminated": True, "turn_completed_observed": True, "final_usage_reconciled": True, "public_command_processes_terminated": True},
                    }, turn_completed_elapsed_ms=2.0, exact_command="claude -p",
                    schema_version="repopact.experiment-run.v3", runner_contract_version="repopact.real-runner.v3",
                )

        from benchmarks.harness.empirical_workspace import WORKSPACE_SECURITY_VERSION
        security = {"version": WORKSPACE_SECURITY_VERSION, "fingerprint": "a" * 64, "preflight": {"contained": True}}
        with tempfile.TemporaryDirectory() as temp, patch("benchmarks.harness.empirical.EmpiricalWorkspace.validate_for_inference", return_value=security):
            turn = EmpiricalExecutor(
                model=IDENTITY, cwd=temp, capture_root=temp, pricing_id="not-exposed",
                output_schema=SCHEMA, transport=FakeTransport(), workspace_root=temp,
            ).run("read", capture_name="capture.json", study_id="S", case_id="C", condition="baseline", fixture="f", fixture_version="v", workspace_identity={"security": True})
        envelope = turn.to_envelope(study_id="S", case_id="C", condition="baseline", fixture="f", fixture_version="v", repetition=0, seed=0, scorer_version="s", success=True)
        self.assertEqual(envelope.schema_version, "repopact.experiment-run.v3")
        self.assertIsNone(envelope.aggregate.usd)
        self.assertIsNone(envelope.aggregate.reasoning_output_tokens)


if __name__ == "__main__":
    unittest.main()
