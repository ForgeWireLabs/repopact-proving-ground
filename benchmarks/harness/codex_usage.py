"""Request-level usage ledger for the public Codex app-server protocol."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TOKENIZER_PACKAGE = "tiktoken"
TOKENIZER_VERSION = "0.9.0"
TOKENIZER_ENCODING = "o200k_base"


class UsageLedgerError(ValueError):
    """A Codex usage notification cannot be reconciled without guessing."""


@dataclass(frozen=True)
class UsageBreakdown:
    input_tokens: int
    cached_input_tokens: int
    cache_write_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int
    total_tokens: int

    @classmethod
    def from_app_server(cls, value: Any) -> "UsageBreakdown":
        if not isinstance(value, dict):
            raise UsageLedgerError("app-server usage breakdown must be an object")
        names = {
            "inputTokens": "input_tokens",
            "cachedInputTokens": "cached_input_tokens",
            "cacheWriteInputTokens": "cache_write_input_tokens",
            "outputTokens": "output_tokens",
            "reasoningOutputTokens": "reasoning_output_tokens",
            "totalTokens": "total_tokens",
        }
        values: dict[str, int] = {}
        for source, target in names.items():
            raw = value.get(source, 0 if source == "cacheWriteInputTokens" else None)
            if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
                raise UsageLedgerError(f"usage.{source} must be a non-negative integer")
            values[target] = raw
        if values["cached_input_tokens"] > values["input_tokens"]:
            raise UsageLedgerError("cachedInputTokens exceeds inputTokens")
        return cls(**values)

    def subtract(self, other: "UsageBreakdown") -> "UsageBreakdown":
        values = {
            name: getattr(self, name) - getattr(other, name)
            for name in self.__dataclass_fields__
        }
        if any(value < 0 for value in values.values()):
            raise UsageLedgerError("cumulative usage moved backwards")
        return UsageBreakdown(**values)

    def is_zero(self) -> bool:
        return all(getattr(self, name) == 0 for name in self.__dataclass_fields__)

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


@dataclass(frozen=True)
class AcceptedUsage:
    last: UsageBreakdown
    total: UsageBreakdown
    sequence: int


class UsageLedger:
    """Accept only advancing cumulative totals compatible with ``last``.

    Codex can emit duplicate notifications. A duplicate total is ignored. Any partial
    advance, reset, backwards movement, or mismatch between cumulative delta and last
    usage fails closed because reconstructing request-level usage would be speculative.
    """

    def __init__(self) -> None:
        self._total: UsageBreakdown | None = None
        self._sequence = 0
        self.accepted: list[AcceptedUsage] = []

    @property
    def total(self) -> UsageBreakdown | None:
        return self._total

    def accept(self, last: UsageBreakdown, total: UsageBreakdown) -> AcceptedUsage | None:
        previous = self._total or UsageBreakdown(0, 0, 0, 0, 0, 0)
        delta = total.subtract(previous)
        if delta.is_zero():
            if total != previous:
                raise UsageLedgerError("usage total changed without an attributable request")
            return None
        if delta != last:
            raise UsageLedgerError(
                f"cumulative usage delta {delta.to_dict()} does not equal last usage {last.to_dict()}"
            )
        if last.is_zero():
            raise UsageLedgerError("an advancing usage notification cannot have zero last usage")
        self._sequence += 1
        accepted = AcceptedUsage(last=last, total=total, sequence=self._sequence)
        self._total = total
        self.accepted.append(accepted)
        return accepted


def accept_notification(ledger: UsageLedger, message: dict[str, Any]) -> AcceptedUsage | None:
    """Consume one public ``thread/tokenUsage/updated`` JSON-RPC notification."""
    if message.get("method") != "thread/tokenUsage/updated":
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        raise UsageLedgerError("token usage notification params must be an object")
    usage = params.get("tokenUsage")
    if not isinstance(usage, dict):
        raise UsageLedgerError("token usage notification requires tokenUsage")
    return ledger.accept(
        UsageBreakdown.from_app_server(usage.get("last")),
        UsageBreakdown.from_app_server(usage.get("total")),
    )


def task_token_count(task_payload: str) -> int:
    """Count the exact harness-supplied task payload with the pinned BPE encoding."""
    try:
        import importlib.metadata
        import tiktoken
    except ImportError as exc:  # pragma: no cover - packaging gate exercises this
        raise UsageLedgerError("v2 telemetry requires the pinned tiktoken tokenizer") from exc
    installed = importlib.metadata.version(TOKENIZER_PACKAGE)
    if installed != TOKENIZER_VERSION:
        raise UsageLedgerError(f"expected {TOKENIZER_PACKAGE}=={TOKENIZER_VERSION}, found {installed}")
    encoding = tiktoken.get_encoding(TOKENIZER_ENCODING)
    return len(encoding.encode(task_payload, disallowed_special=()))
