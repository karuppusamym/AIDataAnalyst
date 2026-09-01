"""Tool-first execution rate metric (TL-6).

A governance-maturity signal: of an organization's *completed* agent runs,
what fraction were served by a certified `GovernedToolVersion` ("tool-first")
rather than ad-hoc SQL that never touched the tool catalog. No new persisted
state -- this is a pure aggregation over `AgentRun.generation_source`, the
same field `agent_orchestrator.GovernedAgentOrchestrator._generate_sql`
already records on every run (see `agent_orchestrator.py` lines ~551-670)
and that `product_marketplace_api.py`'s own portfolio-trend endpoint already
groups by for its dashboard tiles (`_build_trend_points`,
`get_portfolio_overview`).

`generation_source` takes one of six values:

* ``GOVERNED_TOOL`` -- the run resolved to a published, parameter-validated
  governed tool and rendered its SQL template. This is "tool-first".
* ``MODEL_GATEWAY`` -- no governed tool matched (or none was eligible); SQL
  was generated ad hoc by the model gateway. This is "freeform".
* ``QUERY_MEMORY_ADAPTATION`` -- AG-7: same as ``MODEL_GATEWAY`` (the model
  gateway still generates and `sql_guard` still validates the result; see
  `query_memory.py` and `agent_orchestrator.py`), except the prompt was also
  grounded in a version-checked, structurally similar prior successful
  query. It never touched the tool catalog either, so it counts as
  "freeform" for the same reason ``DEVELOPMENT_OVERRIDE`` does below --
  crediting it as tool-first would misstate governance maturity.
* ``DEVELOPMENT_OVERRIDE`` -- a raw SQL string supplied directly, gated by
  `Settings.allow_development_sql_override` (disabled on a real tenant
  route). Still ad-hoc, non-governed SQL, so it counts as "freeform" too --
  crediting it as tool-first would misstate governance maturity.
* ``PENDING`` -- the run's initial value; never advances past it unless
  generation was attempted, so no `COMPLETED` run carries it.
* ``POLICY_BLOCK`` -- screening rejected the run before SQL was ever
  generated (`agent_orchestrator.py` line 387); no `COMPLETED` run carries
  it either.

Because the last two never appear on a `COMPLETED` run, filtering to
`AgentRun.status == "COMPLETED"` (mirroring TL-4's "only `COMPLETED`
`ToolExecution` rows count -- a rejected/failed attempt is not evidence"
convention in `tool_usage.get_tool_usage_counts`) is sufficient to exclude
them without special-casing here; this module still names them so a caller
passing an unfiltered count map gets a self-documenting, harmlessly-ignored
bucket rather than a silent miscount.

Every factor mirrors this codebase's "every factor inspectable" convention
already used by `aida.connector_health` / `aida.trust_scoring`: the ratio is
never returned without the numerator and denominator that produced it, and
this module has no database dependency so it is fully unit-testable
(`tests/test_tool_first_rate.py`). The DB-facing aggregation that turns
`AgentRun` rows into the `dict[str, int]` this module consumes lives in
`aida.fleet.tool_first_execution_rate`, the only place that touches a
session.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

#: The one `generation_source` value that represents a certified, governed
#: tool serving the execution end to end.
TOOL_FIRST_SOURCE = "GOVERNED_TOOL"

#: `generation_source` values that represent SQL that ran without ever
#: invoking a governed tool -- ad hoc, whether model-generated, memory-
#: adapted (AG-7 -- still model-generated, still `sql_guard`-validated,
#: just grounded in a prior query's structural shape), or a raw development
#: override.
FREEFORM_SOURCES = frozenset(
    {"MODEL_GATEWAY", "QUERY_MEMORY_ADAPTATION", "DEVELOPMENT_OVERRIDE"}
)

#: `generation_source` values that never appear on a `COMPLETED` `AgentRun`
#: (see module docstring) and so are excluded from both the numerator and
#: the denominator even if a caller's count map happens to include them.
EXCLUDED_SOURCES = frozenset({"PENDING", "POLICY_BLOCK"})

#: Every value this module knows how to attribute to either bucket.
COUNTED_SOURCES = FREEFORM_SOURCES | {TOOL_FIRST_SOURCE}

#: Default rolling-window length for the rate, matching TL-4's
#: `tool_usage.DEFAULT_USAGE_LOOKBACK_DAYS` convention of a bounded lookback
#: rather than all-time history.
DEFAULT_WINDOW_DAYS = 30

#: The tracker's own maturity bar (TL-6: "target >=40% in a mature tenant").
#: Exposed as a named constant, not a magic number, so the API and its tests
#: reference the same source of truth.
MATURE_TENANT_TARGET_RATE = 0.40


@dataclass(frozen=True, slots=True)
class ToolFirstRate:
    """Composite tool-first execution rate for one organization/window."""

    organization_id: UUID
    window_days: int
    tool_first_executions: int
    freeform_executions: int
    total_executions: int
    rate: float | None  # None (not 0.0) when total_executions == 0 -- no evidence yet
    by_source: dict[str, int] = field(default_factory=dict)
    target_rate: float = MATURE_TENANT_TARGET_RATE
    meets_target: bool | None = None  # None mirrors rate: no evidence, not "failing"
    computed_at: datetime = field(default_factory=lambda: datetime.now())


def compute_tool_first_rate(
    *,
    organization_id: UUID,
    window_days: int,
    generation_source_counts: dict[str, int],
    now: datetime,
    target_rate: float = MATURE_TENANT_TARGET_RATE,
) -> ToolFirstRate:
    """Derive the tool-first rate from a `{generation_source: count}` map.

    `generation_source_counts` is expected to already be scoped to one
    organization and one rolling window (see
    `aida.fleet.tool_first_execution_rate`), and ideally to `COMPLETED`
    runs only -- but this function is defensive regardless: any source not
    in `COUNTED_SOURCES` (including the excluded `PENDING`/`POLICY_BLOCK`
    values) contributes to neither the numerator nor the denominator, so an
    uncertain/incomplete run can never inflate or deflate the rate.
    """
    counted = {
        source: count
        for source, count in generation_source_counts.items()
        if source in COUNTED_SOURCES and count > 0
    }
    tool_first = counted.get(TOOL_FIRST_SOURCE, 0)
    freeform = sum(count for source, count in counted.items() if source in FREEFORM_SOURCES)
    total = tool_first + freeform
    rate = round(tool_first / total, 4) if total > 0 else None
    return ToolFirstRate(
        organization_id=organization_id,
        window_days=window_days,
        tool_first_executions=tool_first,
        freeform_executions=freeform,
        total_executions=total,
        rate=rate,
        by_source=dict(sorted(counted.items())),
        target_rate=target_rate,
        meets_target=None if rate is None else rate >= target_rate,
        computed_at=now,
    )
