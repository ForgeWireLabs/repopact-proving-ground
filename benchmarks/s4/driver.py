"""S4 condition validation, token/cost aggregation, and registered analyses."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable

try:
    from ..harness.model import TokenUsage
    from ..harness.registry import RegisteredSet, load_registered_set
except ImportError:  # pragma: no cover
    from harness.model import TokenUsage  # type: ignore
    from harness.registry import RegisteredSet, load_registered_set  # type: ignore


SCORER_VERSION = "s4-token-economy.v1"
REGISTERED_CONDITIONS = ("C0", "C1", "C2", "C2+C3", "C3", "C4", "C5", "C6", "C7", "C8", "C9")


@dataclass(frozen=True)
class ContextObservation:
    task_id: str
    condition: str
    requests: tuple[TokenUsage, ...]
    success: bool
    accumulated_project_state: int

    def validate(self) -> None:
        validate_condition(self.condition)
        if self.accumulated_project_state < 0:
            raise ValueError("accumulated_project_state must be non-negative")
        if not self.requests:
            raise ValueError("S4 observations require at least one request")
        for index, usage in enumerate(self.requests):
            if usage.requests != 1:
                raise ValueError(f"request {index} must have requests=1")
            if usage.provider is None or usage.model is None or usage.pricing_id is None:
                raise ValueError("S4 request telemetry requires provider, model, and pricing_id")


@dataclass(frozen=True)
class ParetoPoint:
    condition: str
    cost: float
    success_rate: float
    observations: int = 1


def load_conditions(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    source = Path(path) if path else Path(__file__).with_name("conditions.json")
    data = json.loads(source.read_text(encoding="utf-8"))
    conditions = data.get("conditions")
    if not isinstance(conditions, list) or [item.get("id") for item in conditions] != list(REGISTERED_CONDITIONS):
        raise ValueError("S4 condition registry must contain the registered ordered C0-C9 regimes")
    return {item["id"]: item for item in conditions}


def validate_condition(condition: str, *, allow_out_of_scope: bool = False) -> None:
    conditions = load_conditions()
    if condition not in conditions:
        raise ValueError(f"unknown S4 condition: {condition}")
    if not conditions[condition]["runnable"] and not allow_out_of_scope:
        raise ValueError(f"S4 condition {condition} is registered but out of scope")


def load_task_set(path: str | Path | None = None) -> RegisteredSet:
    source = Path(path) if path else Path(__file__).with_name("task-set.json")
    return load_registered_set(source, study_id="S4", records_key="tasks")


def aggregate_observation(observation: ContextObservation) -> dict[str, Any]:
    observation.validate()
    total = TokenUsage()
    for usage in observation.requests:
        total = total + usage
    request_count = len(observation.requests)
    resolved_cost = round(total.usd, 6) if observation.success else None
    return {
        "task_id": observation.task_id,
        "condition": observation.condition,
        "success": observation.success,
        "input_tokens_per_request": round(total.input_tokens / request_count, 3),
        "output_tokens_per_request": round(total.output_tokens / request_count, 3),
        "context_tokens_per_request": round(total.context_tokens / request_count, 3),
        "task_tokens_per_request": round(total.task_tokens / request_count, 3),
        "cache_adjusted_tokens": total.cache_adjusted_input_tokens,
        "tokens_to_completion": total.input_tokens + total.output_tokens if observation.success else None,
        "requests_per_task": request_count,
        "usd_per_request": round(total.usd / request_count, 6),
        "usd_per_resolved_task": resolved_cost,
        "provider": total.provider,
        "model": total.model,
        "pricing_id": total.pricing_id,
        "accumulated_project_state": observation.accumulated_project_state,
        "scorer_version": SCORER_VERSION,
    }


def pareto_frontier(points: Iterable[ParetoPoint]) -> list[ParetoPoint]:
    """Return non-dominated cost/success points; failed-only points never win."""
    candidates = [point for point in points if point.cost >= 0 and 0 <= point.success_rate <= 1 and point.success_rate > 0]
    frontier: list[ParetoPoint] = []
    for point in candidates:
        dominated = any(
            other is not point and other.cost <= point.cost and other.success_rate >= point.success_rate
            and (other.cost < point.cost or other.success_rate > point.success_rate)
            for other in candidates
        )
        if not dominated:
            frontier.append(point)
    return sorted(frontier, key=lambda point: (point.cost, -point.success_rate, point.condition))


def scaling_curve(observations: Iterable[ContextObservation]) -> list[dict[str, float | int]]:
    grouped: dict[int, list[float]] = defaultdict(list)
    for observation in observations:
        observation.validate()
        total = sum(request.context_tokens for request in observation.requests)
        grouped[observation.accumulated_project_state].append(total / len(observation.requests))
    return [
        {"accumulated_project_state": state, "mean_context_tokens_per_request": round(mean(values), 3), "n": len(values)}
        for state, values in sorted(grouped.items())
    ]


def analyze(observations: Iterable[ContextObservation]) -> dict[str, Any]:
    observations = list(observations)
    rows = [aggregate_observation(observation) for observation in observations]
    by_condition: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_condition[row["condition"]].append(row)
    points = []
    for condition, condition_rows in by_condition.items():
        successes = sum(bool(row["success"]) for row in condition_rows)
        costs = [row["usd_per_request"] * row["requests_per_task"] for row in condition_rows]
        points.append(ParetoPoint(condition, mean(costs), successes / len(condition_rows), len(condition_rows)))
    return {
        "illustrative": True,
        "classification": "illustrative",
        "rows": rows,
        "pareto_frontier": [point.__dict__ for point in pareto_frontier(points)],
        "scaling_curve": scaling_curve(observations),
    }


class S4Driver:
    """Execute context regimes through a supplied runner, then analyze observations."""

    study_id = "S4"

    def __init__(self, task_set: RegisteredSet | None = None) -> None:
        self.task_set = task_set or load_task_set()

    def run_case(self, task: dict[str, Any], condition: str, runner: Any) -> ContextObservation:
        validate_condition(condition)
        observation = runner.run(task, condition, study_spec={"study_id": self.study_id, "scorer_version": SCORER_VERSION})
        if not isinstance(observation, ContextObservation):
            raise TypeError("S4 runner must return ContextObservation")
        observation.validate()
        return observation
