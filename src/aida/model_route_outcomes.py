"""R11-MP11: how each model route has actually done, read from the runs themselves.

DataPilot shows a side-by-side router evaluation over labelled cases. The
benchmarks here (`scripts/quality_benchmark.py`, the execution-match and
calibration scripts) are deterministic and run in CI with no model, so they say
nothing per route. What does say something per route is already recorded on
every Ask run: which route answered (`AgentRun.model_route`), whether the chain
fell back or skipped a cooling-down route (`model_call_attempts`, R11-MP04),
whether the statement needed a repair and whether it came out valid
(`sql_repair`, R11-MP05), how a second candidate agreed (`sql_candidate`,
R11-MP07), and what the provider stated it charged and served from cache
(`model_call_evidence`, R11-MP02).

This module only counts those. It reads a bounded window of one
organization's runs, newest first, and says when the bound cut the window
short rather than presenting a partial count as the whole.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import AgentRun

#: Most runs one summary reads. The window is newest-first, so a cut drops the
#: oldest runs in it, and the answer says it was cut.
OUTCOME_SCAN_LIMIT = 2_000


@dataclass(slots=True)
class RouteOutcome:
    route_key: str
    runs: int = 0
    completed: int = 0
    rejected: int = 0
    failed: int = 0
    fallback_runs: int = 0
    circuit_skips: int = 0
    repairs_attempted: int = 0
    repairs_valid: int = 0
    candidates_compared: int = 0
    candidates_identical: int = 0
    candidates_same_sources: int = 0
    candidates_different: int = 0
    stated_cost_usd: float | None = None
    cached_input_tokens: int = 0


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _add_run(outcome: RouteOutcome, status: str, evidence: Mapping[str, Any]) -> None:
    outcome.runs += 1
    if status == "COMPLETED":
        outcome.completed += 1
    elif status == "REJECTED":
        outcome.rejected += 1
    elif status == "FAILED":
        outcome.failed += 1

    attempts = evidence.get("model_call_attempts")
    if isinstance(attempts, list):
        outcomes = [_mapping(attempt).get("outcome") for attempt in attempts]
        if any(result != "SUCCEEDED" for result in outcomes):
            outcome.fallback_runs += 1
        outcome.circuit_skips += sum(1 for result in outcomes if result == "SKIPPED_CIRCUIT_OPEN")

    repair_attempts = _mapping(evidence.get("sql_repair")).get("attempts")
    if isinstance(repair_attempts, list) and repair_attempts:
        outcome.repairs_attempted += 1
        if _mapping(repair_attempts[-1]).get("result") == "VALID":
            outcome.repairs_valid += 1

    candidate = _mapping(evidence.get("sql_candidate"))
    if candidate.get("result") == "COMPARED":
        outcome.candidates_compared += 1
        level = _mapping(candidate.get("agreement")).get("level")
        if level == "IDENTICAL":
            outcome.candidates_identical += 1
        elif level == "SAME_SOURCES":
            outcome.candidates_same_sources += 1
        elif level == "DIFFERENT":
            outcome.candidates_different += 1

    call = _mapping(evidence.get("model_call_evidence"))
    cost = call.get("provider_reported_cost_usd")
    if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0:
        outcome.stated_cost_usd = (outcome.stated_cost_usd or 0.0) + float(cost)
    cached = call.get("provider_cached_input_tokens")
    if isinstance(cached, int) and not isinstance(cached, bool) and cached > 0:
        outcome.cached_input_tokens += cached


def summarize_route_outcomes(
    runs: Iterable[tuple[str | None, str, Any]],
) -> list[RouteOutcome]:
    """Count `(model_route, status, plan_evidence)` rows per route. Pure.

    A run with no route never reached a model and belongs to no route's record,
    so it is left out rather than filed under a placeholder.
    """
    by_route: dict[str, RouteOutcome] = {}
    for route_key, status, evidence in runs:
        if not route_key:
            continue
        outcome = by_route.setdefault(route_key, RouteOutcome(route_key=route_key))
        _add_run(outcome, status, _mapping(evidence))
    return sorted(by_route.values(), key=lambda outcome: (-outcome.runs, outcome.route_key))


async def load_route_outcomes(
    session: AsyncSession,
    organization_id: UUID,
    *,
    since: datetime,
    limit: int = OUTCOME_SCAN_LIMIT,
) -> tuple[list[RouteOutcome], int, bool]:
    """One organization's route outcomes since `since`: the per-route counts,
    how many runs were read, and whether `limit` cut the window short."""
    rows = (
        await session.execute(
            select(AgentRun.model_route, AgentRun.status, AgentRun.plan_evidence)
            .where(
                AgentRun.organization_id == organization_id,
                AgentRun.created_at >= since,
                AgentRun.model_route.is_not(None),
            )
            .order_by(AgentRun.created_at.desc())
            .limit(limit + 1)
        )
    ).all()
    truncated = len(rows) > limit
    considered = rows[:limit]
    return (
        summarize_route_outcomes((row[0], row[1], row[2]) for row in considered),
        len(considered),
        truncated,
    )
