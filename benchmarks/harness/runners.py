"""Model-agnostic runners and the versioned subprocess contract."""
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import asdict

try:
    from .execution import (
        REAL_RUNNER_CONTRACT_VERSION, RunEnvelope, EnvelopeValidationError, RUN_SCHEMA_VERSION,
        REAL_RUNNER_CONTRACT_VERSION_V2, RUN_SCHEMA_VERSION_V2, ModelIdentity, aggregate_telemetry,
        parse_token_usage, parse_token_usage_v2,
    )
    from .model import AgentAction, Task, TokenUsage
except ImportError:  # pragma: no cover - legacy script entry point
    from execution import (  # type: ignore
        REAL_RUNNER_CONTRACT_VERSION, RunEnvelope, EnvelopeValidationError, RUN_SCHEMA_VERSION,
        REAL_RUNNER_CONTRACT_VERSION_V2, RUN_SCHEMA_VERSION_V2, ModelIdentity, aggregate_telemetry,
        parse_token_usage, parse_token_usage_v2,
    )
    from model import AgentAction, Task, TokenUsage  # type: ignore

try:
    from .grader_v2 import parse_action_signal
except ImportError:  # pragma: no cover - legacy script entry point
    from grader_v2 import parse_action_signal  # type: ignore


class RunnerContractError(ValueError):
    """A subprocess response is malformed or lacks required telemetry."""


def build_request(task: Task, arm: str, study_spec: dict | None = None) -> dict:
    """Build the versioned request sent to any provider wrapper."""
    return {
        "protocol_version": REAL_RUNNER_CONTRACT_VERSION,
        "task": {
            "task_id": task.id,
            "title": task.title,
            "category": task.category,
            "polarity": task.polarity,
            "frozen_surface": task.frozen_surface,
            "security_class": task.security_class,
            "fixture": task.fixture,
            "prompt": task.prompt,
            "metadata": task.metadata,
        },
        "condition": arm,
        "study_spec": study_spec or {},
    }


def build_request_v2(task: Task, arm: str, study_spec: dict | None = None) -> dict:
    request = build_request(task, arm, study_spec)
    request["protocol_version"] = REAL_RUNNER_CONTRACT_VERSION_V2
    request["response_contract"] = {
        "version": "pactbench.action-signal.v1",
        "signal_kinds": ["blocked", "escalated", "proceeded_safely", "violated_silently", "errored"],
        "blocked_requires": "deterministic_enforcer_evidence",
        "escalated_requires": "explicit_approval_request",
    }
    return request


def _required_bool(action: dict, name: str) -> bool:
    if name not in action or not isinstance(action[name], bool):
        raise RunnerContractError(f"action.{name} must be a boolean")
    return action[name]


def parse_response(payload: object, task: Task, arm: str, *, exact_command: str) -> AgentAction:
    """Parse a complete RealRunner response without silently zero-filling telemetry."""
    if not isinstance(payload, dict):
        raise RunnerContractError("runner response must be a JSON object")
    if payload.get("protocol_version") != REAL_RUNNER_CONTRACT_VERSION:
        raise RunnerContractError(f"protocol_version must be {REAL_RUNNER_CONTRACT_VERSION}")
    status = payload.get("status")
    if status not in {"completed", "incomplete", "failed"}:
        raise RunnerContractError("status must be completed, incomplete, or failed")
    action = payload.get("action")
    if not isinstance(action, dict):
        raise RunnerContractError("action must be an object")
    model = payload.get("model")
    if not isinstance(model, dict):
        raise RunnerContractError("model must be an object")
    for name in ("family", "provider", "version"):
        if not isinstance(model.get(name), str) or not model[name].strip():
            raise RunnerContractError(f"model.{name} must be a non-empty string")
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise RunnerContractError("provenance must be an object")
    for name in ("task_set_version", "fixture_version"):
        if not isinstance(provenance.get(name), str) or not provenance[name].strip():
            raise RunnerContractError(f"provenance.{name} must be a non-empty string")
    capture = payload.get("capture")
    if not isinstance(capture, dict) or not isinstance(capture.get("raw_transcript_ref"), str) or not capture["raw_transcript_ref"].strip():
        raise RunnerContractError("capture.raw_transcript_ref is required")
    observations = payload.get("observations")
    if not isinstance(observations, dict):
        raise RunnerContractError("observations must be an object")
    telemetry = payload.get("telemetry")
    if not isinstance(telemetry, dict):
        if status == "completed":
            raise RunnerContractError("telemetry.requests must be an array")
        telemetry = {"requests": []}
    if not isinstance(telemetry.get("requests"), list):
        raise RunnerContractError("telemetry.requests must be an array")

    requests: list[TokenUsage] = []
    for index, request in enumerate(telemetry["requests"]):
        try:
            parsed = parse_token_usage(request, path=f"telemetry.requests[{index}]")
        except EnvelopeValidationError as exc:
            raise RunnerContractError(str(exc)) from exc
        for name in ("provider", "model", "pricing_id"):
            if getattr(parsed, name) in {None, ""}:
                raise RunnerContractError(f"telemetry.requests[{index}].{name} is required")
        requests.append(parsed)
    if status == "completed" and not requests:
        raise RunnerContractError("completed response requires per-request telemetry")
    aggregate_data = telemetry.get("aggregate")
    if not isinstance(aggregate_data, dict):
        if status == "completed" or requests:
            raise RunnerContractError("telemetry.aggregate is required")
        # A failed run may have no telemetry at all. This zero-valued object is attached
        # to an explicit failure and is never eligible for empirical aggregation.
        aggregate = TokenUsage()
    else:
        try:
            aggregate = parse_token_usage(
                aggregate_data, path="telemetry.aggregate", requests=sum(r.requests for r in requests)
            )
        except EnvelopeValidationError as exc:
            raise RunnerContractError(str(exc)) from exc
    expected = aggregate_telemetry(requests)
    if asdict(expected) != asdict(aggregate):
        raise RunnerContractError("telemetry.aggregate does not equal per-request telemetry")
    if status != "completed":
        failure = payload.get("failure")
        if not isinstance(failure, dict) or not isinstance(failure.get("class"), str) or not failure["class"].strip():
            raise RunnerContractError("incomplete/failed response requires failure.class")
        failure_class = failure["class"]
    else:
        if payload.get("failure") is not None:
            raise RunnerContractError("completed response cannot carry failure")
        failure_class = None

    result = AgentAction(
        weakened_guarantee=_required_bool(action, "weakened_guarantee"),
        escalated=_required_bool(action, "escalated"),
        blocked_by_enforcer=_required_bool(action, "blocked_by_enforcer"),
        completed_task=_required_bool(action, "completed_task"),
        errored=(status != "completed") or _required_bool(action, "errored"),
        tokens=aggregate,
        note=action.get("note", "") if isinstance(action.get("note", ""), str) else str(action["note"]),
        failure_class=failure_class,
        observations=observations,
    )
    envelope = RunEnvelope(
        schema_version=RUN_SCHEMA_VERSION,
        study_id=str(provenance.get("study_id", "unknown")),
        case_id=task.id,
        condition=arm,
        fixture=task.fixture or "unknown",
        fixture_version=provenance["fixture_version"],
        repetition=int(provenance.get("repetition", 0)),
        seed=provenance.get("seed", 0),
        model=ModelIdentity(model["family"], model["provider"], model["version"]),
        temperature_policy=str(provenance.get("temperature_policy", "provider-default")),
        scorer_version=str(provenance.get("scorer_version", "unspecified")),
        started_at=provenance.get("started_at"),
        ended_at=provenance.get("ended_at"),
        elapsed_ms=provenance.get("elapsed_ms"),
        completed=status == "completed",
        success=status == "completed" and bool(action.get("completed_task") or action.get("escalated") or action.get("blocked_by_enforcer")),
        failure_class=failure_class,
        per_request=requests,
        aggregate=aggregate,
        observations=observations,
        raw_capture_ref=capture["raw_transcript_ref"],
        exact_command=exact_command,
        provenance={**provenance, "capture": capture},
        notes=result.note,
        illustrative=False,
    )
    result.envelope = envelope
    return result


class MockRunner:
    """Deterministic test double. Its output is plumbing evidence only."""

    name = "mock"

    def run(self, task: Task, arm: str, study_spec: dict | None = None) -> AgentAction:
        ctx = 1800 if arm == "repopact" else 1200
        tokens = TokenUsage(
            input_tokens=ctx + 600, output_tokens=350, context_tokens=ctx,
            task_tokens=600, requests=1, usd=round((ctx + 950) * 1e-5, 6),
            cached_tokens=0, cache_adjusted_input_tokens=ctx + 600,
            pricing_id="illustrative/mock-v1", provider="mock", model="mock-v1",
            elapsed_ms=1.0,
        )
        if task.polarity == "legitimate":
            return AgentAction(completed_task=True, tokens=tokens,
                               note="illustrative legitimate change applied")
        if arm == "repopact":
            if task.frozen_surface:
                return AgentAction(blocked_by_enforcer=True, tokens=tokens,
                                   note="illustrative frozen-surface block; --ack required")
            return AgentAction(escalated=True, tokens=tokens,
                               note="illustrative binding-invariant escalation")
        return AgentAction(weakened_guarantee=True, tokens=tokens,
                           note="illustrative baseline shortcut")


class RealRunner:
    """Drive a live agent subprocess using the versioned JSON contract."""

    name = "real"

    def __init__(self, cmd: str | None = None, *, timeout_seconds: int = 1800) -> None:
        self.cmd = cmd or os.environ.get("REPOPACT_AGENT_CMD")
        self.timeout_seconds = timeout_seconds

    def run(self, task: Task, arm: str, study_spec: dict | None = None) -> AgentAction:
        if not self.cmd:
            raise NotImplementedError(
                "RealRunner needs REPOPACT_AGENT_CMD; the wrapper must implement "
                f"{REAL_RUNNER_CONTRACT_VERSION} and provide complete telemetry."
            )
        request = build_request(task, arm, study_spec)
        try:
            proc = subprocess.run(
                self.cmd, shell=True, input=json.dumps(request), capture_output=True,
                text=True, timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RunnerContractError(f"runner timed out after {self.timeout_seconds}s") from exc
        if proc.returncode != 0:
            raise RunnerContractError(f"agent exit {proc.returncode}: {proc.stderr[:200]}")
        try:
            payload = json.loads(proc.stdout)
        except (ValueError, json.JSONDecodeError) as exc:
            raise RunnerContractError("agent did not emit valid JSON") from exc
        return parse_response(payload, task, arm, exact_command=self.cmd)


def parse_response_v2(payload: object, task: Task, arm: str, *, exact_command: str) -> AgentAction:
    """Parse the frozen v2 response with request-level complete telemetry."""
    if not isinstance(payload, dict):
        raise RunnerContractError("runner response must be a JSON object")
    if payload.get("protocol_version") != REAL_RUNNER_CONTRACT_VERSION_V2:
        raise RunnerContractError(f"protocol_version must be {REAL_RUNNER_CONTRACT_VERSION_V2}")
    status = payload.get("status")
    if status not in {"completed", "incomplete", "failed"}:
        raise RunnerContractError("status must be completed, incomplete, or failed")
    action = payload.get("action")
    if not isinstance(action, dict):
        raise RunnerContractError("action must be an object")
    try:
        signal = parse_action_signal(action.get("signal"))
    except ValueError as exc:
        raise RunnerContractError(str(exc)) from exc
    model = payload.get("model")
    if not isinstance(model, dict) or any(not isinstance(model.get(name), str) or not model[name].strip() for name in ("family", "provider", "version")):
        raise RunnerContractError("model family/provider/version are required")
    provenance = payload.get("provenance")
    if not isinstance(provenance, dict):
        raise RunnerContractError("provenance must be an object")
    for name in ("study_id", "task_set_version", "fixture_version", "scorer_version"):
        if not isinstance(provenance.get(name), str) or not provenance[name].strip():
            raise RunnerContractError(f"provenance.{name} is required")
    capture = payload.get("capture")
    if not isinstance(capture, dict) or not isinstance(capture.get("raw_transcript_ref"), str) or not capture["raw_transcript_ref"].strip():
        raise RunnerContractError("capture.raw_transcript_ref is required")
    observations = payload.get("observations")
    if not isinstance(observations, dict):
        raise RunnerContractError("observations must be an object")
    telemetry = payload.get("telemetry")
    if not isinstance(telemetry, dict) or not isinstance(telemetry.get("requests"), list):
        raise RunnerContractError("telemetry.requests must be an array")
    requests: list[TokenUsage] = []
    for index, request in enumerate(telemetry["requests"]):
        try:
            requests.append(parse_token_usage_v2(request, path=f"telemetry.requests[{index}]"))
        except EnvelopeValidationError as exc:
            raise RunnerContractError(str(exc)) from exc
    if status == "completed" and not requests:
        raise RunnerContractError("completed response requires per-request telemetry")
    aggregate_data = telemetry.get("aggregate")
    if not isinstance(aggregate_data, dict):
        raise RunnerContractError("telemetry.aggregate is required")
    try:
        aggregate = parse_token_usage_v2(
            aggregate_data, path="telemetry.aggregate", requests=sum(item.requests for item in requests)
        )
    except EnvelopeValidationError as exc:
        raise RunnerContractError(str(exc)) from exc
    expected_aggregate = aggregate_telemetry(requests)
    if requests:
        if asdict(expected_aggregate) != asdict(aggregate):
            raise RunnerContractError("telemetry.aggregate does not equal per-request telemetry")
    else:
        numeric_fields = (
            "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
            "reasoning_output_tokens", "context_tokens", "task_tokens", "cache_adjusted_input_tokens",
            "requests", "tool_calls", "elapsed_ms", "usd",
        )
        if any(getattr(aggregate, name) != 0 for name in numeric_fields):
            raise RunnerContractError("zero-request telemetry.aggregate must be numerically zero")
    failure_class: str | None = None
    if status != "completed":
        failure = payload.get("failure")
        if not isinstance(failure, dict) or not isinstance(failure.get("class"), str) or not failure["class"].strip():
            raise RunnerContractError("incomplete/failed response requires failure.class")
        failure_class = failure["class"]
    envelope = RunEnvelope(
        schema_version=RUN_SCHEMA_VERSION_V2,
        study_id=provenance["study_id"], case_id=task.id, condition=arm,
        fixture=task.fixture or "unknown", fixture_version=provenance["fixture_version"],
        repetition=int(provenance.get("repetition", 0)), seed=provenance.get("seed", 0),
        model=ModelIdentity(model["family"], model["provider"], model["version"]),
        temperature_policy=str(provenance.get("temperature_policy", "provider-default")),
        scorer_version=provenance["scorer_version"], started_at=provenance.get("started_at"),
        ended_at=provenance.get("ended_at"), elapsed_ms=provenance.get("elapsed_ms"),
        completed=status == "completed", success=status == "completed" and signal["kind"] != "errored",
        failure_class=failure_class, per_request=requests, aggregate=aggregate,
        observations=observations, raw_capture_ref=capture["raw_transcript_ref"],
        exact_command=exact_command, provenance={**provenance, "capture": capture},
        notes=str(action.get("note", "")), illustrative=False,
    )
    return AgentAction(
        weakened_guarantee=signal["kind"] == "violated_silently",
        escalated=signal["kind"] == "escalated",
        blocked_by_enforcer=signal["kind"] == "blocked",
        completed_task=signal["kind"] == "proceeded_safely",
        errored=status != "completed" or signal["kind"] == "errored",
        tokens=aggregate, note=str(action.get("note", "")), failure_class=failure_class,
        observations=observations, envelope=envelope, action_signal=signal,
    )


class RealRunnerV2:
    """Drive a narrow v2 provider adapter; v1 remains available for historical runs."""

    name = "real-v2"

    def __init__(self, cmd: str | None = None, *, timeout_seconds: int = 1800) -> None:
        self.cmd = cmd or os.environ.get("REPOPACT_AGENT_CMD_V2")
        self.timeout_seconds = timeout_seconds

    def run(self, task: Task, arm: str, study_spec: dict | None = None) -> AgentAction:
        if not self.cmd:
            raise NotImplementedError(
                "RealRunnerV2 needs REPOPACT_AGENT_CMD_V2; the wrapper must implement "
                f"{REAL_RUNNER_CONTRACT_VERSION_V2}."
            )
        request = build_request_v2(task, arm, study_spec)
        try:
            proc = subprocess.run(self.cmd, shell=True, input=json.dumps(request), capture_output=True, text=True, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise RunnerContractError(f"runner timed out after {self.timeout_seconds}s") from exc
        if proc.returncode != 0:
            raise RunnerContractError(f"agent exit {proc.returncode}: {proc.stderr[:200]}")
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise RunnerContractError("agent did not emit valid JSON") from exc
        return parse_response_v2(payload, task, arm, exact_command=self.cmd)


def get_runner(name: str):
    return {"mock": MockRunner, "real": RealRunner, "real-v2": RealRunnerV2}[name]()
