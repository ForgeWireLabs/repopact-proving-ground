"""Shared empirical Codex executor for the AC-3 study adapters.

The executor owns transport, public request accounting, strict structured output,
capture integrity, and the common empirical envelope.  Study modules own fixture
materialization, postconditions, and scoring; they must not manufacture telemetry
from a task-level estimate.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .capture import assert_no_secrets
from .codex_app_server import AppServerRun, CodexAppServer
from .codex_usage import AcceptedUsage, UsageLedgerError, task_token_count
from .execution import (
    REAL_RUNNER_CONTRACT_VERSION_V2,
    RUN_SCHEMA_VERSION_V2,
    ModelIdentity,
    RunEnvelope,
    aggregate_telemetry,
    require_valid_envelope,
)
from .model import TokenUsage


EMPIRICAL_EXECUTOR_VERSION = "repopact.codex-empirical.v1"
PUBLIC_APP_SERVER_COMMAND = "codex app-server --stdio"
PUBLIC_USAGE_SOURCE = "public Codex app-server thread/tokenUsage/updated"
TEMPERATURE_POLICY = "provider-default"
_TOOL_ITEM_TYPES = frozenset({
    "commandExecution", "fileChange", "mcpToolCall", "dynamicToolCall", "functionCall",
})


class EmpiricalContractError(ValueError):
    """The empirical run cannot be accepted without inventing evidence."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tool_call_count(events: tuple[dict[str, Any], ...], start_index: int, end_index: int) -> int:
    count = 0
    for event in events[start_index + 1:end_index]:
        if event.get("method") != "item/completed":
            continue
        params = event.get("params")
        item = params.get("item") if isinstance(params, dict) else None
        if isinstance(item, dict) and item.get("type") in _TOOL_ITEM_TYPES:
            count += 1
    return count


def _provider_usage(app_run: AppServerRun) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for accepted, elapsed_ms, event_index in app_run.usage:
        result.append({
            "sequence": accepted.sequence,
            "notification_event_index": event_index,
            "elapsed_ms_observed": round(elapsed_ms, 3),
            "last": accepted.last.to_dict(),
            "total": accepted.total.to_dict(),
        })
    for event in app_run.events:
        if event.get("method") != "thread/tokenUsage/updated":
            continue
        params = event.get("params")
        usage = params.get("tokenUsage") if isinstance(params, dict) else None
        if isinstance(usage, dict) and isinstance(usage.get("modelContextWindow"), int):
            result.append({"model_context_window": usage["modelContextWindow"]})
    return result


def telemetry_from_app_run(
    app_run: AppServerRun,
    task_payload: str,
    *,
    identity: ModelIdentity,
    pricing_id: str,
) -> tuple[list[TokenUsage], dict[str, Any]]:
    """Convert accepted public usage deltas into the frozen v2 telemetry shape."""
    if not app_run.usage:
        raise UsageLedgerError("public app-server run contained no accepted request-level usage")
    task_tokens = task_token_count(task_payload)
    requests: list[TokenUsage] = []
    previous_elapsed = 0.0
    for index, (accepted, elapsed_ms, event_index) in enumerate(app_run.usage):
        raw = accepted.last
        request_task_tokens = task_tokens if index == 0 else 0
        if request_task_tokens > raw.input_tokens:
            raise UsageLedgerError("task token attribution exceeds provider input")
        next_event_index = app_run.usage[index + 1][2] if index + 1 < len(app_run.usage) else len(app_run.events)
        interval_elapsed = max(0.0, elapsed_ms - previous_elapsed)
        previous_elapsed = elapsed_ms
        requests.append(TokenUsage(
            input_tokens=raw.input_tokens,
            output_tokens=raw.output_tokens,
            context_tokens=raw.input_tokens - request_task_tokens,
            task_tokens=request_task_tokens,
            requests=1,
            usd=0.0,
            cached_tokens=raw.cached_input_tokens,
            cached_input_tokens=raw.cached_input_tokens,
            cache_write_input_tokens=raw.cache_write_input_tokens,
            reasoning_output_tokens=raw.reasoning_output_tokens,
            cache_adjusted_input_tokens=raw.input_tokens - raw.cached_input_tokens,
            pricing_id=pricing_id,
            provider=identity.provider,
            model=identity.version,
            tool_calls=_tool_call_count(app_run.events, event_index, next_event_index),
            elapsed_ms=round(interval_elapsed, 3),
        ))
    aggregate = aggregate_telemetry(requests)
    return requests, {
        "source": PUBLIC_USAGE_SOURCE,
        "accepted_request_count": len(requests),
        "task_token_attribution": {
            "first_request_only": True,
            "tokenizer_package": "tiktoken",
            "tokenizer_version": "0.9.0",
            "encoding": "o200k_base",
            "task_tokens": task_tokens,
        },
        "provider_usage": _provider_usage(app_run),
        "aggregate": asdict(aggregate),
        "pricing_id": pricing_id,
        "usd_policy": "public Codex app-server does not expose a rate card here; USD is recorded as 0 and never inferred",
    }


def _safe_capture_path(root: Path, name: str) -> tuple[Path, str]:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise EmpiricalContractError("capture name must be a relative path inside capture_root")
    destination = (root / relative).resolve()
    root_resolved = root.resolve()
    if destination != root_resolved and root_resolved not in destination.parents:
        raise EmpiricalContractError("capture path escapes capture_root")
    return destination, relative.as_posix()


@dataclass(frozen=True)
class EmpiricalTurn:
    """One real model turn plus all evidence needed to reproduce its accounting."""

    model: ModelIdentity
    provider: str
    thread_id: str
    turn_id: str
    raw_events: tuple[dict[str, Any], ...]
    server_requests: tuple[dict[str, Any], ...]
    final_output: Any
    final_output_text: str
    per_request: tuple[TokenUsage, ...]
    aggregate: TokenUsage
    elapsed_ms: float
    tool_calls: int
    telemetry: dict[str, Any]
    capture_ref: str
    capture_digest: str
    runtime_identity: dict[str, Any]
    schema_identity: dict[str, Any]
    provenance: dict[str, Any]

    def validate(self) -> None:
        if self.provenance.get("classification") != "empirical":
            raise EmpiricalContractError("empirical turn provenance must be classified empirical")
        if self.provenance.get("executor_version") != EMPIRICAL_EXECUTOR_VERSION:
            raise EmpiricalContractError("empirical turn provenance has no recognized executor version")
        if not self.capture_ref or len(self.capture_digest) != 64:
            raise EmpiricalContractError("empirical turn requires a capture reference and digest")
        if not self.runtime_identity.get("command") or not self.runtime_identity.get("initialize_result"):
            raise EmpiricalContractError("empirical turn requires public runtime identity")
        if len(self.schema_identity.get("schema_digest", "")) != 64:
            raise EmpiricalContractError("empirical turn requires schema identity")
        if not self.per_request or self.aggregate.requests != len(self.per_request):
            raise EmpiricalContractError("empirical turn requires reconciled request telemetry")

    def to_envelope(
        self,
        *,
        study_id: str,
        case_id: str,
        condition: str,
        fixture: str,
        fixture_version: str,
        repetition: int,
        seed: int | str,
        scorer_version: str,
        success: bool,
        observations: dict[str, Any] | None = None,
        failure_class: str | None = None,
        notes: str = "",
    ) -> RunEnvelope:
        self.validate()
        envelope = RunEnvelope(
            schema_version=RUN_SCHEMA_VERSION_V2,
            study_id=study_id,
            case_id=case_id,
            condition=condition,
            fixture=fixture,
            fixture_version=fixture_version,
            repetition=repetition,
            seed=seed,
            model=self.model,
            temperature_policy=TEMPERATURE_POLICY,
            scorer_version=scorer_version,
            started_at=self.provenance.get("started_at"),
            ended_at=self.provenance.get("ended_at"),
            elapsed_ms=self.elapsed_ms,
            completed=True,
            success=success,
            failure_class=failure_class,
            per_request=list(self.per_request),
            aggregate=self.aggregate,
            observations=observations or {},
            raw_capture_ref=self.capture_ref,
            exact_command=PUBLIC_APP_SERVER_COMMAND,
            provenance={**self.provenance, "capture_digest": self.capture_digest},
            notes=notes,
            illustrative=False,
        )
        return require_valid_envelope(envelope, empirical=True)


class EmpiricalExecutor:
    """Run one isolated public app-server turn and write a secret-checked capture."""

    def __init__(
        self,
        *,
        model: ModelIdentity,
        cwd: str | Path,
        capture_root: str | Path,
        pricing_id: str,
        timeout_seconds: int = 1800,
        output_schema: dict[str, Any],
        runtime_version: str = "codex-cli-public-app-server-v2",
    ) -> None:
        if (
            not isinstance(output_schema, dict)
            or output_schema.get("type") != "object"
            or output_schema.get("additionalProperties") is not False
            or not isinstance(output_schema.get("required"), list)
        ):
            raise EmpiricalContractError("empirical output_schema must be a strict object schema")
        self.model = model
        self.cwd = str(Path(cwd).resolve())
        self.capture_root = Path(capture_root)
        self.pricing_id = pricing_id
        self.timeout_seconds = timeout_seconds
        self.output_schema = output_schema
        self.runtime_version = runtime_version

    def run(
        self,
        prompt: str,
        *,
        capture_name: str,
        study_id: str,
        case_id: str,
        condition: str,
        fixture: str,
        fixture_version: str,
        repetition: int = 0,
        seed: int | str = 0,
        workspace_identity: dict[str, Any] | None = None,
        auxiliary_calls: tuple[dict[str, Any], ...] = (),
    ) -> EmpiricalTurn:
        if not isinstance(prompt, str) or not prompt.strip():
            raise EmpiricalContractError("empirical prompt must be non-empty")
        if not workspace_identity:
            raise EmpiricalContractError("empirical run requires workspace identity")
        destination, capture_ref = _safe_capture_path(self.capture_root, capture_name)
        started_at = _now()
        app_run = CodexAppServer(
            model=self.model.version,
            provider=self.model.provider,
            cwd=self.cwd,
            timeout_seconds=self.timeout_seconds,
            output_schema=self.output_schema,
        ).run(prompt)
        ended_at = _now()
        try:
            structured = json.loads(app_run.final_output)
        except (TypeError, json.JSONDecodeError) as exc:
            raise EmpiricalContractError("final app-server output was not JSON") from exc
        try:
            from jsonschema import validate
            validate(instance=structured, schema=self.output_schema)
        except ImportError as exc:  # pragma: no cover - packaging gate
            raise EmpiricalContractError("jsonschema is required for empirical output validation") from exc
        except Exception as exc:
            raise EmpiricalContractError(f"final output failed empirical schema validation: {exc}") from exc
        requests, telemetry = telemetry_from_app_run(
            app_run, prompt, identity=self.model, pricing_id=self.pricing_id,
        )
        aggregate = aggregate_telemetry(requests)
        runtime_identity = {
            "executor_version": EMPIRICAL_EXECUTOR_VERSION,
            "runtime_version": self.runtime_version,
            "command": PUBLIC_APP_SERVER_COMMAND,
            "initialize_result": app_run.initialize_result,
            "thread_result": app_run.thread_result,
        }
        schema_identity = {
            "schema_digest": _digest(self.output_schema),
            "schema": self.output_schema,
        }
        provenance = {
            "classification": "empirical",
            "executor_version": EMPIRICAL_EXECUTOR_VERSION,
            "runner_contract_version": REAL_RUNNER_CONTRACT_VERSION_V2,
            "study_id": study_id,
            "case_id": case_id,
            "condition": condition,
            "fixture": fixture,
            "fixture_version": fixture_version,
            "model": asdict(self.model),
            "workspace_identity": workspace_identity,
            "auxiliary_calls": list(auxiliary_calls),
            "started_at": started_at,
            "ended_at": ended_at,
        }
        payload = {
            "capture_schema_version": "repopact.empirical-turn-capture.v1",
            "classification": "empirical",
            "provenance": provenance,
            "runtime_identity": runtime_identity,
            "schema_identity": schema_identity,
            "thread_id": app_run.thread_id,
            "turn_id": app_run.turn_id,
            "model": asdict(self.model),
            "prompt": prompt,
            "final_output": structured,
            "final_output_text": app_run.final_output,
            "raw_events": list(app_run.events),
            "server_requests": list(app_run.server_requests),
            "per_request": [asdict(item) for item in requests],
            "aggregate": asdict(aggregate),
            "telemetry": telemetry,
            "elapsed_ms": app_run.elapsed_ms,
            "tool_calls": aggregate.tool_calls,
            "capture_ref": capture_ref,
        }
        assert_no_secrets(payload)
        capture_digest = _digest(payload)
        payload["capture_digest"] = capture_digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        provenance["capture_digest"] = capture_digest
        provenance["capture_ref"] = capture_ref
        turn = EmpiricalTurn(
            model=self.model,
            provider=self.model.provider,
            thread_id=app_run.thread_id,
            turn_id=app_run.turn_id,
            raw_events=app_run.events,
            server_requests=app_run.server_requests,
            final_output=structured,
            final_output_text=app_run.final_output,
            per_request=tuple(requests),
            aggregate=aggregate,
            elapsed_ms=app_run.elapsed_ms,
            tool_calls=aggregate.tool_calls,
            telemetry=telemetry,
            capture_ref=capture_ref,
            capture_digest=capture_digest,
            runtime_identity=runtime_identity,
            schema_identity=schema_identity,
            provenance=provenance,
        )
        turn.validate()
        return turn
