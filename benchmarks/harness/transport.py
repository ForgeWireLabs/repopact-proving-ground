"""Provider-neutral empirical transports.

The transport owns only process/protocol concerns.  ``EmpiricalExecutor`` remains
the owner of the study prompt contract, workspace boundary, capture, schema
validation, secret scan, and common envelope.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .codex_app_server import CodexAppServer
from .execution import (
    ModelIdentity,
    REAL_RUNNER_CONTRACT_VERSION_V2,
    REAL_RUNNER_CONTRACT_VERSION_V3,
    RUN_SCHEMA_VERSION_V2,
    RUN_SCHEMA_VERSION_V3,
    aggregate_telemetry,
)
from .model import TokenUsage
from .codex_usage import UsageLedgerError


class EmpiricalTransportError(RuntimeError):
    """A provider runtime could not produce an admissible empirical run."""


@dataclass(frozen=True)
class TransportRun:
    """Normalized output of one provider runtime invocation."""

    model: ModelIdentity
    events: tuple[dict[str, Any], ...]
    final_output: str
    server_requests: tuple[dict[str, Any], ...]
    thread_id: str
    turn_id: str
    elapsed_ms: float
    per_request: tuple[TokenUsage, ...]
    telemetry: dict[str, Any]
    runtime_identity: dict[str, Any]
    turn_completed_elapsed_ms: float | None
    exact_command: str
    schema_version: str
    runner_contract_version: str


class EmpiricalTransport(Protocol):
    """The provider-neutral transport contract consumed by EmpiricalExecutor."""

    def run(self, prompt: str, *, output_schema: dict[str, Any]) -> TransportRun:
        ...


class CodexAppServerTransport:
    """Adapter preserving the published Codex app-server v2 semantics."""

    name = "codex-app-server"

    def __init__(
        self,
        *,
        model: ModelIdentity,
        cwd: str,
        pricing_id: str,
        timeout_seconds: int,
    ) -> None:
        self.model = model
        self.cwd = cwd
        self.pricing_id = pricing_id
        self.timeout_seconds = timeout_seconds

    def run(self, prompt: str, *, output_schema: dict[str, Any]) -> TransportRun:
        # Imported lazily to keep the historical telemetry function's public
        # location and the legacy test patch path intact.
        from .empirical import telemetry_from_app_run

        app_run = CodexAppServer(
            model=self.model.version,
            provider=self.model.provider,
            cwd=self.cwd,
            timeout_seconds=self.timeout_seconds,
            output_schema=output_schema,
        ).run(prompt)
        requests, telemetry = telemetry_from_app_run(
            app_run, prompt, identity=self.model, pricing_id=self.pricing_id,
        )
        runtime_identity = {
            "transport": self.name,
            "runtime_version": "codex-cli-public-app-server-v2",
            "command": "codex app-server --stdio",
            "initialize_result": app_run.initialize_result,
            "thread_result": app_run.thread_result,
            "process_lifecycle": app_run.process_lifecycle or {},
        }
        return TransportRun(
            model=self.model,
            events=app_run.events,
            final_output=app_run.final_output,
            server_requests=app_run.server_requests,
            thread_id=app_run.thread_id,
            turn_id=app_run.turn_id,
            elapsed_ms=app_run.elapsed_ms,
            per_request=tuple(requests),
            telemetry=telemetry,
            runtime_identity=runtime_identity,
            turn_completed_elapsed_ms=app_run.turn_completed_elapsed_ms,
            exact_command="codex app-server --stdio",
            schema_version=RUN_SCHEMA_VERSION_V2,
            runner_contract_version=REAL_RUNNER_CONTRACT_VERSION_V2,
        )


def _as_int(value: Any, *, field: str, event_index: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise EmpiricalTransportError(f"Claude usage field {field} at event {event_index} is not a non-negative integer")
    return value


def _first_int(obj: dict[str, Any], names: tuple[str, ...], *, event_index: int) -> tuple[int | None, str | None]:
    for name in names:
        if name in obj:
            return _as_int(obj[name], field=name, event_index=event_index), name
    return None, None


def _cache_creation_tokens(usage: dict[str, Any], *, event_index: int) -> tuple[int, str]:
    value, field = _first_int(
        usage,
        ("cache_creation_input_tokens", "cacheCreationInputTokens", "cache_write_input_tokens"),
        event_index=event_index,
    )
    if value is not None:
        return value, field or "cache_creation_input_tokens"
    nested = usage.get("cache_creation") or usage.get("cacheCreation")
    if isinstance(nested, dict):
        total = 0
        seen = False
        for key, raw in nested.items():
            if "input_tokens" in key or "InputTokens" in key:
                total += _as_int(raw, field=f"cache_creation.{key}", event_index=event_index)
                seen = True
        if seen:
            return total, "cache_creation.*_input_tokens"
    return 0, "not-exposed"


def _message_usage(event: dict[str, Any]) -> dict[str, Any] | None:
    usage = event.get("usage")
    if isinstance(usage, dict):
        return usage
    message = event.get("message")
    if isinstance(message, dict) and isinstance(message.get("usage"), dict):
        return message["usage"]
    return None


def _event_model(event: dict[str, Any]) -> str | None:
    for candidate in (event.get("model"), (event.get("message") or {}).get("model") if isinstance(event.get("message"), dict) else None):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _event_text(event: dict[str, Any]) -> str | None:
    value = event.get("result")
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "content"):
            if isinstance(value.get(key), str):
                return value[key]
    return None


def _tool_count(events: list[dict[str, Any]]) -> int:
    count = 0
    for event in events:
        if event.get("type") == "tool_use":
            count += 1
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if isinstance(content, list):
            count += sum(isinstance(item, dict) and item.get("type") == "tool_use" for item in content)
    return count


def _session_id(events: list[dict[str, Any]], result: dict[str, Any]) -> str:
    for event in events:
        for key in ("session_id", "sessionId"):
            if isinstance(event.get(key), str) and event[key]:
                return event[key]
    for key in ("session_id", "sessionId"):
        if isinstance(result.get(key), str) and result[key]:
            return result[key]
    raise EmpiricalTransportError("Claude Code output did not expose a session identity")


class ClaudeCodeTransport:
    """Strict adapter for Claude Code's documented headless stream-json surface."""

    name = "claude-code"

    def __init__(
        self,
        *,
        model: ModelIdentity,
        cwd: str,
        pricing_id: str,
        timeout_seconds: int,
        executable: str = "claude",
    ) -> None:
        if model.provider != "anthropic" or model.family != "claude-sonnet-5" or model.version != "claude-sonnet-5":
            raise EmpiricalTransportError("ClaudeCodeTransport requires the exact claude-sonnet-5/anthropic/claude-sonnet-5 identity")
        self.model = model
        self.cwd = str(Path(cwd).resolve())
        self.pricing_id = pricing_id
        self.timeout_seconds = timeout_seconds
        self.executable = executable

    def _command(self) -> list[str]:
        # ``-p`` reads the prompt from stdin when no positional prompt is given.
        # These are all public Claude Code flags; acceptEdits is recorded as
        # provider-native provenance and is not presented as RepoPact behavior.
        return [
            self.executable, "-p",
            "--output-format", "stream-json",
            "--verbose",
            "--model", self.model.version,
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Read,Write,Edit,Bash",
            "--no-session-persistence",
        ]

    def run(self, prompt: str, *, output_schema: dict[str, Any]) -> TransportRun:
        command = self._command()
        started = time.monotonic()
        try:
            process = subprocess.Popen(
                command,
                cwd=self.cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
        except OSError as exc:
            raise EmpiricalTransportError(f"could not start Claude Code executable {self.executable!r}: {exc}") from exc
        try:
            stdout, stderr = process.communicate(prompt, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            stdout, stderr = process.communicate()
            raise EmpiricalTransportError(f"Claude Code timed out after {self.timeout_seconds}s; process was terminated") from exc
        elapsed_ms = round((time.monotonic() - started) * 1000.0, 3)
        lines = [line for line in stdout.splitlines() if line.strip()]
        events: list[dict[str, Any]] = []
        for line_number, line in enumerate(lines, start=1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EmpiricalTransportError(f"Claude Code emitted non-JSON stream data on line {line_number}") from exc
            if not isinstance(value, dict):
                raise EmpiricalTransportError(f"Claude Code stream event on line {line_number} is not an object")
            events.append(value)
        result_events = [event for event in events if event.get("type") == "result"]
        if not result_events:
            raise EmpiricalTransportError("Claude Code stream ended without a result event")
        result = result_events[-1]
        if process.returncode != 0:
            raise EmpiricalTransportError(f"Claude Code exited nonzero ({process.returncode}): {stderr[:400]}")
        if result.get("is_error") is True or result.get("subtype") not in {None, "success"}:
            raise EmpiricalTransportError(f"Claude Code reported a failed result: {result.get('subtype') or result.get('is_error')}")

        reported_models = []
        for event in events:
            candidate = _event_model(event)
            if candidate:
                reported_models.append(candidate)
        if not reported_models:
            raise EmpiricalTransportError("Claude Code output did not expose an exact model identity")
        if any(candidate != self.model.version for candidate in reported_models):
            raise EmpiricalTransportError(f"Claude Code reported model identity {reported_models!r}, expected {self.model.version!r}")

        request_events: list[tuple[int, dict[str, Any]]] = []
        for index, event in enumerate(events):
            if event.get("type") not in {"assistant", "message_start", "message_delta"}:
                continue
            usage = _message_usage(event)
            if usage is not None:
                request_events.append((index, usage))
        if not request_events:
            aggregate_usage = result.get("usage") or result.get("modelUsage")
            raise EmpiricalTransportError(
                "Claude Code exposed no request/response usage event; aggregate usage "
                f"surface {type(aggregate_usage).__name__} is not admissible for AC-2 per-request telemetry"
            )

        from .empirical import _digest
        from .codex_usage import task_token_count

        task_tokens = task_token_count(prompt)
        requests: list[TokenUsage] = []
        raw_usage: list[dict[str, Any]] = []
        total_cost = result.get("total_cost_usd")
        if total_cost is None:
            total_cost = result.get("totalCostUsd")
        if total_cost is not None and (not isinstance(total_cost, (int, float)) or isinstance(total_cost, bool) or total_cost < 0):
            raise EmpiricalTransportError("Claude Code total cost is not a non-negative number")
        for request_index, (event_index, usage) in enumerate(request_events):
            uncached_input, input_field = _first_int(usage, ("input_tokens", "inputTokens"), event_index=event_index)
            output_tokens, output_field = _first_int(usage, ("output_tokens", "outputTokens"), event_index=event_index)
            if uncached_input is None or output_tokens is None:
                raise EmpiricalTransportError(f"Claude request event {event_index} lacks input_tokens/output_tokens")
            cache_read, cache_read_field = _first_int(
                usage, ("cache_read_input_tokens", "cacheReadInputTokens", "cached_input_tokens"), event_index=event_index,
            )
            cache_write, cache_write_field = _cache_creation_tokens(usage, event_index=event_index)
            cache_read = cache_read or 0
            # Anthropic reports input_tokens separately from cache reads/creation;
            # the normalized input is their explicit sum, never an inferred zero.
            normalized_input = uncached_input + cache_read + cache_write
            request_task_tokens = task_tokens if request_index == 0 else 0
            if request_task_tokens > normalized_input:
                raise EmpiricalTransportError("task token attribution exceeds Claude input telemetry")
            reasoning, reasoning_field = _first_int(
                usage,
                ("reasoning_output_tokens", "reasoningOutputTokens", "thinking_tokens", "thinkingTokens"),
                event_index=event_index,
            )
            request_cost = usage.get("cost_usd", usage.get("costUSD"))
            if request_cost is not None and (not isinstance(request_cost, (int, float)) or isinstance(request_cost, bool) or request_cost < 0):
                raise EmpiricalTransportError(f"Claude request event {event_index} cost is not a non-negative number")
            # A result-level cost cannot be allocated across several response
            # messages without inventing a split.  Preserve it in metadata and
            # only attach it to a single-response request.
            if request_cost is None and len(request_events) == 1 and total_cost is not None:
                request_cost = total_cost
            raw_usage.append({
                "event_index": event_index,
                "input_tokens": uncached_input,
                "input_field": input_field,
                "output_tokens": output_tokens,
                "output_field": output_field,
                "cache_read_input_tokens": cache_read,
                "cache_read_field": cache_read_field or "not-exposed",
                "cache_creation_input_tokens": cache_write,
                "cache_creation_field": cache_write_field,
                "reasoning_output_tokens": reasoning,
                "reasoning_field": reasoning_field or "not-exposed",
                "cost_usd": request_cost,
            })
            requests.append(TokenUsage(
                input_tokens=normalized_input,
                output_tokens=output_tokens,
                context_tokens=normalized_input - request_task_tokens,
                task_tokens=request_task_tokens,
                requests=1,
                usd=request_cost,
                cached_tokens=cache_read,
                cached_input_tokens=cache_read,
                cache_write_input_tokens=cache_write,
                reasoning_output_tokens=reasoning,
                cache_adjusted_input_tokens=uncached_input,
                pricing_id=self.pricing_id,
                provider=self.model.provider,
                model=self.model.version,
                tool_calls=_tool_count(events),
                elapsed_ms=elapsed_ms if request_index == len(request_events) - 1 else 0.0,
            ))
        aggregate = aggregate_telemetry(requests)
        session = _session_id(events, result)
        init = next((event for event in events if event.get("type") == "system" and event.get("subtype") == "init"), {})
        runtime_version = init.get("claude_code_version") or init.get("version") or "unknown"
        lifecycle = {
            "owned_process_pid": process.pid,
            "owned_process_command": command,
            "process_terminated": process.poll() is not None,
            "returncode": process.returncode,
            "turn_completed_observed": True,
            "final_usage_reconciled": True,
            "public_command_processes_terminated": True,
            "accepted_usage_count": len(requests),
        }
        runtime_identity = {
            "transport": self.name,
            "runtime_version": runtime_version,
            "command": " ".join(command),
            "command_argv": command,
            "initialize_result": init,
            "session_id": session,
            "reported_model": self.model.version,
            "permission_mode": "acceptEdits",
            "allowed_tools": ["Read", "Write", "Edit", "Bash"],
            "process_lifecycle": lifecycle,
            "stderr": stderr,
        }
        telemetry = {
            "source": "Claude Code documented stream-json assistant usage events",
            "request_granularity": "one normalized request record per assistant/message usage event",
            "provider_usage": raw_usage,
            "aggregate": aggregate.__dict__,
            "session_id": session,
            "total_cost_usd_reported": total_cost,
            "cost_status": "client-estimate" if total_cost is not None else "not-exposed",
            "usd_policy": "Claude Code total_cost_usd is preserved as a client-side estimate; no zero is substituted and no rate-card inference is performed",
            "unavailable_fields": sorted({
                field for item in raw_usage for field, value in (
                    ("cache_read_input_tokens", item["cache_read_input_tokens"]),
                    ("cache_creation_input_tokens", item["cache_creation_input_tokens"]),
                    ("reasoning_output_tokens", item["reasoning_output_tokens"]),
                    ("cost_usd", item["cost_usd"]),
                ) if value is None or (field.startswith("cache_") and item.get(field.replace("_input_tokens", "_field")) == "not-exposed")
            }),
            "model_usage_surface": result.get("modelUsage"),
            "result_event": {key: result.get(key) for key in ("type", "subtype", "session_id", "model", "num_turns", "duration_ms") if key in result},
        }
        return TransportRun(
            model=self.model,
            events=tuple(events),
            final_output=_event_text(result) or "",
            server_requests=(),
            thread_id=session,
            turn_id=session,
            elapsed_ms=elapsed_ms,
            per_request=tuple(requests),
            telemetry=telemetry,
            runtime_identity=runtime_identity,
            turn_completed_elapsed_ms=elapsed_ms,
            exact_command=" ".join(command),
            schema_version=RUN_SCHEMA_VERSION_V3,
            runner_contract_version=REAL_RUNNER_CONTRACT_VERSION_V3,
        )


def build_transport(*, model: ModelIdentity, cwd: str, pricing_id: str, timeout_seconds: int) -> EmpiricalTransport:
    if model.provider == "anthropic":
        return ClaudeCodeTransport(model=model, cwd=cwd, pricing_id=pricing_id, timeout_seconds=timeout_seconds)
    return CodexAppServerTransport(model=model, cwd=cwd, pricing_id=pricing_id, timeout_seconds=timeout_seconds)
