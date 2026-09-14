"""Study-neutral execution and capture primitives.

The benchmark studies have different outcomes, but they share the mechanics of an
experiment run.  This module deliberately contains only that common envelope and
its validation; study-specific observations stay in their own drivers.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

try:  # Support both ``python -m benchmarks.harness...`` and the legacy script entry point.
    from .model import TokenUsage
except ImportError:  # pragma: no cover - exercised by the documented script command
    from model import TokenUsage


RUN_SCHEMA_VERSION = "repopact.experiment-run.v1"
REAL_RUNNER_CONTRACT_VERSION = "repopact.real-runner.v1"
RUN_SCHEMA_VERSION_V2 = "repopact.experiment-run.v2"
REAL_RUNNER_CONTRACT_VERSION_V2 = "repopact.real-runner.v2"


class EnvelopeValidationError(ValueError):
    """Raised when a run envelope cannot be treated as reproducible telemetry."""


@dataclass(frozen=True)
class ModelIdentity:
    family: str
    provider: str
    version: str


@dataclass
class RunEnvelope:
    """Common experiment/run record shared by S1/S6a and S2-S6 drivers.

    ``success`` means the requested execution completed successfully.  A study's
    scientific outcome belongs in ``observations``; for example, an S5 mutation can
    complete successfully while correctly demonstrating a known blind spot.
    """

    schema_version: str
    study_id: str
    case_id: str
    condition: str
    fixture: str
    fixture_version: str
    repetition: int
    seed: int | str
    model: ModelIdentity
    temperature_policy: str
    scorer_version: str
    started_at: str | None
    ended_at: str | None
    elapsed_ms: float | None
    completed: bool
    success: bool
    failure_class: str | None
    per_request: list[TokenUsage]
    aggregate: TokenUsage
    observations: dict[str, Any] = field(default_factory=dict)
    raw_capture_ref: str | None = None
    exact_command: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)
    notes: str = ""
    illustrative: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["model"] = asdict(self.model)
        data["per_request"] = [asdict(t) for t in self.per_request]
        data["aggregate"] = asdict(self.aggregate)
        return data


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def aggregate_telemetry(requests: list[TokenUsage]) -> TokenUsage:
    """Aggregate request telemetry without filling missing values with zeros."""
    if not requests:
        return TokenUsage()
    total = TokenUsage()
    for request in requests:
        total = total + request
    return total


def validate_token_usage(usage: TokenUsage, *, path: str = "tokens") -> list[str]:
    problems: list[str] = []
    integer_fields = (
        "input_tokens", "output_tokens", "context_tokens", "task_tokens",
        "cached_tokens", "cached_input_tokens", "cache_write_input_tokens", "reasoning_output_tokens",
        "cache_adjusted_input_tokens", "requests", "tool_calls",
    )
    for name in integer_fields:
        value = getattr(usage, name)
        if not isinstance(value, int) or isinstance(value, bool):
            problems.append(f"{path}.{name} must be an integer")
        elif value < 0:
            problems.append(f"{path}.{name} must be non-negative")
    if not isinstance(usage.usd, (int, float)) or isinstance(usage.usd, bool):
        problems.append(f"{path}.usd must be a number")
    elif usage.usd < 0:
        problems.append(f"{path}.usd must be non-negative")
    if not isinstance(usage.elapsed_ms, (int, float)) or isinstance(usage.elapsed_ms, bool):
        problems.append(f"{path}.elapsed_ms must be a number")
    elif usage.elapsed_ms < 0:
        problems.append(f"{path}.elapsed_ms must be non-negative")
    valid_integers = all(isinstance(getattr(usage, name), int) and not isinstance(getattr(usage, name), bool) for name in integer_fields)
    if valid_integers and usage.requests == 0 and any(
        getattr(usage, name) for name in ("input_tokens", "output_tokens", "context_tokens", "task_tokens")
    ):
        problems.append(f"{path}.requests cannot be zero when token telemetry is present")
    if valid_integers and usage.context_tokens + usage.task_tokens > usage.input_tokens:
        problems.append(f"{path}.context_tokens + task_tokens exceeds input_tokens")
    return problems


def validate_envelope(envelope: RunEnvelope, *, empirical: bool | None = None) -> list[str]:
    """Return deterministic validation diagnostics for a common run envelope."""
    problems: list[str] = []
    required_strings = (
        "schema_version", "study_id", "case_id", "condition", "fixture",
        "fixture_version", "temperature_policy", "scorer_version",
    )
    for name in required_strings:
        if not isinstance(getattr(envelope, name), str) or not getattr(envelope, name).strip():
            problems.append(f"{name} must be a non-empty string")
    if envelope.schema_version not in {RUN_SCHEMA_VERSION, RUN_SCHEMA_VERSION_V2}:
        problems.append(f"schema_version must be {RUN_SCHEMA_VERSION} or {RUN_SCHEMA_VERSION_V2}")
    if not isinstance(envelope.repetition, int) or isinstance(envelope.repetition, bool) or envelope.repetition < 0:
        problems.append("repetition must be a non-negative integer")
    if not isinstance(envelope.completed, bool) or not isinstance(envelope.success, bool):
        problems.append("completed and success must be booleans")
    if envelope.success and not envelope.completed:
        problems.append("success cannot be true for an incomplete run")
    if not envelope.completed and not envelope.failure_class:
        problems.append("incomplete runs require failure_class")
    if envelope.completed and envelope.failure_class and envelope.success:
        problems.append("successful runs cannot carry failure_class")
    if envelope.elapsed_ms is not None and envelope.elapsed_ms < 0:
        problems.append("elapsed_ms must be non-negative")
    if not isinstance(envelope.model, ModelIdentity):
        problems.append("model must be a ModelIdentity")
    for index, usage in enumerate(envelope.per_request):
        problems.extend(validate_token_usage(usage, path=f"per_request[{index}].tokens"))
        if usage.requests != 1:
            problems.append(f"per_request[{index}].tokens.requests must equal 1")
    problems.extend(validate_token_usage(envelope.aggregate, path="aggregate"))
    expected = aggregate_telemetry(envelope.per_request)
    if asdict(expected) != asdict(envelope.aggregate):
        problems.append("aggregate telemetry does not equal the sum of per_request telemetry")
    is_empirical = (not envelope.illustrative) if empirical is None else empirical
    if is_empirical:
        if not envelope.raw_capture_ref:
            problems.append("empirical runs require raw_capture_ref")
        if not envelope.exact_command:
            problems.append("empirical runs require exact_command")
        if envelope.model.family in {"", "unknown"} or envelope.model.provider in {"", "unknown"} or envelope.model.version in {"", "unknown"}:
            problems.append("empirical runs require exact model family/provider/version")
        if not envelope.per_request and envelope.completed:
            problems.append("completed empirical runs require per-request telemetry")
    else:
        if not envelope.illustrative:
            problems.append("non-illustrative runs must be validated as empirical")
        if envelope.provenance.get("classification") not in {"illustrative", "non_empirical"}:
            problems.append("illustrative runs require provenance.classification")
    return problems


def require_valid_envelope(envelope: RunEnvelope, *, empirical: bool | None = None) -> RunEnvelope:
    problems = validate_envelope(envelope, empirical=empirical)
    if problems:
        raise EnvelopeValidationError("; ".join(problems))
    return envelope


def _require_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EnvelopeValidationError(f"{path} must be an object")
    return value


def parse_token_usage(data: Any, *, path: str, requests: int = 1) -> TokenUsage:
    """Parse complete request telemetry; absent fields are invalid, not zero."""
    obj = _require_mapping(data, path)
    required = ("input_tokens", "output_tokens", "context_tokens", "task_tokens", "usd")
    missing = [name for name in required if name not in obj]
    if missing:
        raise EnvelopeValidationError(f"{path} missing required telemetry: {', '.join(missing)}")
    try:
        usage = TokenUsage(
            input_tokens=obj["input_tokens"], output_tokens=obj["output_tokens"],
            context_tokens=obj["context_tokens"], task_tokens=obj["task_tokens"],
            requests=obj.get("requests", requests), usd=obj["usd"],
            cached_tokens=obj.get("cached_tokens", 0),
            cache_adjusted_input_tokens=obj.get("cache_adjusted_input_tokens", obj["input_tokens"]),
            pricing_id=obj.get("pricing_id"), provider=obj.get("provider"),
            model=obj.get("model"), tool_calls=obj.get("tool_calls", 0),
            elapsed_ms=obj.get("elapsed_ms", 0.0),
        )
    except (TypeError, ValueError) as exc:
        raise EnvelopeValidationError(f"{path} has invalid telemetry types: {exc}") from exc
    problems = validate_token_usage(usage, path=path)
    if problems:
        raise EnvelopeValidationError("; ".join(problems))
    return usage


def parse_token_usage_v2(data: Any, *, path: str, requests: int = 1) -> TokenUsage:
    """Parse the frozen v2 provider ledger without zero-filling absent fields."""
    obj = _require_mapping(data, path)
    required = (
        "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
        "reasoning_output_tokens", "context_tokens", "task_tokens",
        "cache_adjusted_input_tokens", "usd", "pricing_id", "provider", "model",
    )
    missing = [name for name in required if name not in obj]
    if missing:
        raise EnvelopeValidationError(f"{path} missing required v2 telemetry: {', '.join(missing)}")
    try:
        input_tokens = obj["input_tokens"]
        cached_input_tokens = obj["cached_input_tokens"]
        cache_adjusted = obj["cache_adjusted_input_tokens"]
        if not all(isinstance(obj[name], int) and not isinstance(obj[name], bool) for name in (
            "input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens",
            "reasoning_output_tokens", "context_tokens", "task_tokens", "cache_adjusted_input_tokens",
        )):
            raise TypeError("v2 token counts must be integers")
        if cached_input_tokens > input_tokens:
            raise ValueError("cached_input_tokens exceeds input_tokens")
        if cache_adjusted != input_tokens - cached_input_tokens:
            raise ValueError("cache_adjusted_input_tokens must equal input_tokens - cached_input_tokens")
        usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=obj["output_tokens"],
            context_tokens=obj["context_tokens"],
            task_tokens=obj["task_tokens"],
            requests=obj.get("requests", requests),
            usd=obj["usd"],
            cached_tokens=cached_input_tokens,
            cached_input_tokens=cached_input_tokens,
            cache_write_input_tokens=obj["cache_write_input_tokens"],
            reasoning_output_tokens=obj["reasoning_output_tokens"],
            cache_adjusted_input_tokens=cache_adjusted,
            pricing_id=obj["pricing_id"],
            provider=obj["provider"],
            model=obj["model"],
            tool_calls=obj.get("tool_calls", 0),
            elapsed_ms=obj.get("elapsed_ms", 0.0),
        )
    except (TypeError, ValueError) as exc:
        raise EnvelopeValidationError(f"{path} has invalid v2 telemetry: {exc}") from exc
    problems = validate_token_usage(usage, path=path)
    if problems:
        raise EnvelopeValidationError("; ".join(problems))
    return usage
