"""Core types for the benchmark harness."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Outcome(str, Enum):
    """The PactBench confusion-matrix cells (see pactbench/TASK-FORMAT.md)."""
    VIOLATED_SILENTLY = "violated_silently"
    BLOCKED = "blocked"
    ESCALATED = "escalated"
    PROCEEDED_SAFELY = "proceeded_safely"
    FALSE_STOPPED = "false_stopped"
    ERRORED = "errored"


# Outcomes that count as a "catch" on a must_not_weaken task.
CAUGHT = {Outcome.BLOCKED, Outcome.ESCALATED, Outcome.PROCEEDED_SAFELY}


@dataclass
class TokenUsage:
    """Per-request/aggregate token + cost instrumentation (S4 / H11)."""
    input_tokens: int = 0
    output_tokens: int = 0
    context_tokens: int = 0   # the slice spent orienting (vs the task itself)
    task_tokens: int = 0
    requests: int = 0
    # ``None`` is reserved for provider runtimes that do not expose a monetary
    # value.  It is never serialized as a fabricated zero.
    usd: float | None = 0.0
    cached_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_input_tokens: int = 0
    # Claude Code may expose thinking content without a separately metered
    # reasoning count.  ``None`` records that absence; Codex remains integer
    # valued as in the published v2 contract.
    reasoning_output_tokens: int | None = 0
    cache_adjusted_input_tokens: int = 0
    pricing_id: str | None = None
    provider: str | None = None
    model: str | None = None
    tool_calls: int = 0
    elapsed_ms: float = 0.0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            context_tokens=self.context_tokens + other.context_tokens,
            task_tokens=self.task_tokens + other.task_tokens,
            requests=self.requests + other.requests,
            usd=(round(self.usd + other.usd, 6) if self.usd is not None and other.usd is not None else None),
            cached_tokens=self.cached_tokens + other.cached_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_input_tokens=self.cache_write_input_tokens + other.cache_write_input_tokens,
            reasoning_output_tokens=(
                self.reasoning_output_tokens + other.reasoning_output_tokens
                if self.reasoning_output_tokens is not None and other.reasoning_output_tokens is not None
                else None
            ),
            cache_adjusted_input_tokens=self.cache_adjusted_input_tokens + other.cache_adjusted_input_tokens,
            pricing_id=self.pricing_id or other.pricing_id,
            provider=self.provider or other.provider,
            model=self.model or other.model,
            tool_calls=self.tool_calls + other.tool_calls,
            elapsed_ms=round(self.elapsed_ms + other.elapsed_ms, 3),
        )


@dataclass
class Task:
    """A loaded PactBench task (the fields the harness needs to run + grade)."""
    id: str
    title: str
    category: str            # "correctness" | "security"
    polarity: str            # "must_not_weaken" | "legitimate"
    frozen_surface: bool
    arms: list[str]
    security_class: str | None = None
    fixture: str | None = None
    prompt: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class AgentAction:
    """What a runner reports the agent did on one (task, arm) run.

    The grader maps this to an Outcome; the runner never assigns the Outcome itself.
    """
    weakened_guarantee: bool = False   # took the shortcut and committed it
    escalated: bool = False            # asked for approval / used the escalation path
    blocked_by_enforcer: bool = False  # an enforcer prevented the change
    completed_task: bool = False       # resolved the task without weakening anything
    errored: bool = False
    tokens: TokenUsage = field(default_factory=TokenUsage)
    note: str = ""
    failure_class: str | None = None
    observations: dict = field(default_factory=dict)
    envelope: object | None = None
    action_signal: dict | None = None


@dataclass
class RunResult:
    task_id: str
    arm: str
    outcome: Outcome
    polarity: str
    tokens: TokenUsage
    note: str = ""
    envelope: object | None = None
