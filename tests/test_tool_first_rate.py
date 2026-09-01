"""TL-6 -- tool-first execution rate metric (`aida.tool_first_rate`).

Pure-logic tests, no database: the rate is a deterministic function of a
plain `{generation_source: count}` map, mirroring
`tests/test_connector_health.py`'s own "pure logic tested without a
database" convention. The DB-facing aggregation (`aida.fleet.
tool_first_execution_rate`) has its own integration test in
`tests/test_operational_behaviors.py`.
"""

from datetime import UTC, datetime
from uuid import uuid4

from aida.tool_first_rate import (
    FREEFORM_SOURCES,
    MATURE_TENANT_TARGET_RATE,
    TOOL_FIRST_SOURCE,
    compute_tool_first_rate,
)

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def test_all_governed_tool_is_one_hundred_percent() -> None:
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"GOVERNED_TOOL": 12},
        now=NOW,
    )
    assert result.tool_first_executions == 12
    assert result.freeform_executions == 0
    assert result.total_executions == 12
    assert result.rate == 1.0
    assert result.meets_target is True


def test_all_model_gateway_is_zero_percent() -> None:
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"MODEL_GATEWAY": 8},
        now=NOW,
    )
    assert result.tool_first_executions == 0
    assert result.freeform_executions == 8
    assert result.total_executions == 8
    assert result.rate == 0.0
    assert result.meets_target is False


def test_mixed_sources_computes_ratio() -> None:
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"GOVERNED_TOOL": 40, "MODEL_GATEWAY": 60},
        now=NOW,
    )
    assert result.total_executions == 100
    assert result.rate == 0.40
    # Exactly at the target -- >= is inclusive.
    assert result.meets_target is True


def test_development_override_counts_as_freeform_not_tool_first() -> None:
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"GOVERNED_TOOL": 3, "DEVELOPMENT_OVERRIDE": 7},
        now=NOW,
    )
    assert result.tool_first_executions == 3
    assert result.freeform_executions == 7
    assert result.total_executions == 10
    assert result.rate == 0.30


def test_both_freeform_sources_combine_in_the_denominator() -> None:
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={
            "GOVERNED_TOOL": 5,
            "MODEL_GATEWAY": 3,
            "DEVELOPMENT_OVERRIDE": 2,
        },
        now=NOW,
    )
    assert result.freeform_executions == 5
    assert result.total_executions == 10
    assert set(FREEFORM_SOURCES) == {
        "MODEL_GATEWAY",
        "QUERY_MEMORY_ADAPTATION",
        "DEVELOPMENT_OVERRIDE",
    }


def test_query_memory_adaptation_counts_as_freeform_not_tool_first() -> None:
    """AG-7: memory-adapted SQL still went through the model gateway and
    `sql_guard`, never a certified governed tool, so it must not inflate the
    tool-first rate -- same reasoning `DEVELOPMENT_OVERRIDE` already gets."""
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"GOVERNED_TOOL": 3, "QUERY_MEMORY_ADAPTATION": 7},
        now=NOW,
    )
    assert result.tool_first_executions == 3
    assert result.freeform_executions == 7
    assert result.total_executions == 10
    assert result.rate == 0.30


def test_no_executions_returns_none_rate_not_zero() -> None:
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={},
        now=NOW,
    )
    assert result.total_executions == 0
    assert result.rate is None
    assert result.meets_target is None


def test_excluded_sources_never_enter_numerator_or_denominator() -> None:
    # PENDING/POLICY_BLOCK never appear on a COMPLETED AgentRun in practice
    # (see module docstring), but a defensive caller passing them anyway
    # must not have them silently counted either way.
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"GOVERNED_TOOL": 4, "PENDING": 99, "POLICY_BLOCK": 50},
        now=NOW,
    )
    assert result.total_executions == 4
    assert result.rate == 1.0


def test_unknown_source_is_ignored_rather_than_crashing() -> None:
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"GOVERNED_TOOL": 2, "SOMETHING_NEW": 5},
        now=NOW,
    )
    assert result.total_executions == 2
    assert result.rate == 1.0


def test_zero_counts_are_dropped_from_by_source() -> None:
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"GOVERNED_TOOL": 5, "MODEL_GATEWAY": 0},
        now=NOW,
    )
    assert result.by_source == {"GOVERNED_TOOL": 5}


def test_by_source_reflects_every_counted_bucket() -> None:
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={
            "GOVERNED_TOOL": 5,
            "MODEL_GATEWAY": 3,
            "DEVELOPMENT_OVERRIDE": 1,
        },
        now=NOW,
    )
    assert result.by_source == {
        "DEVELOPMENT_OVERRIDE": 1,
        "GOVERNED_TOOL": 5,
        "MODEL_GATEWAY": 3,
    }


def test_meets_target_uses_the_tracker_bar_by_default() -> None:
    assert MATURE_TENANT_TARGET_RATE == 0.40
    just_below = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"GOVERNED_TOOL": 39, "MODEL_GATEWAY": 61},
        now=NOW,
    )
    just_at = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"GOVERNED_TOOL": 40, "MODEL_GATEWAY": 60},
        now=NOW,
    )
    assert just_below.meets_target is False
    assert just_at.meets_target is True


def test_custom_target_rate_is_honored() -> None:
    result = compute_tool_first_rate(
        organization_id=uuid4(),
        window_days=30,
        generation_source_counts={"GOVERNED_TOOL": 50, "MODEL_GATEWAY": 50},
        now=NOW,
        target_rate=0.60,
    )
    assert result.rate == 0.50
    assert result.meets_target is False
    assert result.target_rate == 0.60


def test_result_carries_the_requested_window_and_organization() -> None:
    org_id = uuid4()
    result = compute_tool_first_rate(
        organization_id=org_id,
        window_days=7,
        generation_source_counts={TOOL_FIRST_SOURCE: 1},
        now=NOW,
    )
    assert result.organization_id == org_id
    assert result.window_days == 7
    assert result.computed_at == NOW
