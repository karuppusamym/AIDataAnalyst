"""R11-FP01: a reviewed trigger edge a classification could not travel is counted in the register.

`collect_propagation_inputs` has said, since triggers reached classification propagation, which
reviewed trigger edges it could not turn into a column-level edge -- `TABLE_STAR`, an unbound
firing row, a column the catalog does not hold, two columns that differ only by case -- and
declares each one a gap rather than tagging the written table at table grain. It logged them and
returned them. Nothing counted them: a steward reading the footprint register saw a source with
no gaps while a PII column stopped at an audit trigger. These tests pin the count and the list:

* the register's count and the detail view's list are what propagation returns for
  `TRIGGER_DEFINITION` -- derived from the collector itself, so they cannot drift from it;
* one entry per trigger, however many of its edges are gapped, with every reason it carries;
* a since-dropped trigger's reviewed edges still propagate, so they are still gaps, and listed;
* nothing else is counted: a resolvable edge, an undecided one, a filter-only one, an unparsed
  marker, a routine's gap, another source's, another organization's, a source the caller may not
  read;
* nothing existing moves: an estate without such a gap reads exactly as it did.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

import aida.footprint_gaps as footprint_gaps_module
from aida.authorization_gate import AuthorizationDenied
from aida.classification_propagation import (
    GAP_COLUMN_AMBIGUOUS,
    GAP_COLUMN_NOT_IN_CATALOG,
    GAP_SOURCE_UNRESOLVED,
    GAP_TABLE_STAR,
    collect_propagation_inputs,
)
from aida.footprint_gap_detail import footprint_gap_objects
from aida.footprint_gaps import GAP_DEFINITIONS, footprint_gaps
from tests.support.task_agents import (
    agent_settings,
    human,
    seed_estate,
    seed_table,
    task_agent_session,
)
from tests.test_footprint_gaps import NOW, _edge, _routine
from tests.test_trigger_downstream import _add, _column, _trigger, _trigger_edge

KIND = "TRIGGER_PROPAGATION_GAPS"


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


async def _register(session: AsyncSession, org: Any) -> Any:
    return await footprint_gaps(
        session,
        context=human(org, "ops-1", frozenset({"Operations"})),
        settings=agent_settings(),
        organization_id=org.id,
        now=NOW,
    )


def _kinds(result: Any, datasource_id: UUID) -> dict[str, int]:
    (listed,) = [item for item in result.datasources if item.datasource_id == datasource_id]
    return {gap.kind: gap.count for gap in listed.gaps}


async def _estate(session: AsyncSession) -> dict[str, Any]:
    """One source with three triggers in gap and one without, and everything that must not count."""
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    orders = await seed_table(session, org, datasource, schema, name="orders")
    audit = await seed_table(session, org, datasource, schema, name="orders_audit")
    await _column(session, orders, "ssn", classification="PII")
    await _column(session, audit, "ssn")
    await _column(session, audit, "Email", ordinal=2)
    await _column(session, audit, "EMAIL", ordinal=3)
    await _column(session, orders, "email", ordinal=2)

    # A star copy (the canonical audit trigger) and a column the catalog does not hold: one trigger,
    # two gapped edges, two reasons.
    star_and_ghost = _trigger(org, datasource, schema, name="audit_star", table_name="orders")
    # An unbound firing row (Oracle `:NEW`).
    unbound = _trigger(org, datasource, schema, name="audit_unbound", table_name="orders")
    # The source has since dropped it; what it copied is still in the table it wrote.
    dropped = _trigger(
        org, datasource, schema, name="audit_dropped", table_name="orders", status="DEPRECATED"
    )
    # Resolves cleanly: propagation carries it, so it is no gap.
    clean = _trigger(org, datasource, schema, name="audit_clean", table_name="orders")
    # Nothing here is a gap: undecided, filter-only, an UNPARSED marker and a hop into plumbing.
    quiet = _trigger(org, datasource, schema, name="audit_quiet", table_name="orders")
    await _add(session, star_and_ghost, unbound, dropped, clean, quiet)

    star = _trigger_edge(
        star_and_ghost, orders, audit, source_column="*", target_column="*",
        transformation_type="TABLE_STAR",
    )
    ghost = _trigger_edge(
        star_and_ghost, orders, audit, source_column="phone", target_column="phone"
    )
    await _add(
        session,
        star,
        ghost,
        _trigger_edge(unbound, None, audit),
        _trigger_edge(
            dropped, orders, audit, source_column="*", target_column="*",
            transformation_type="TABLE_STAR",
        ),
        _trigger_edge(clean, orders, audit, source_column="ssn", target_column="ssn"),
        _trigger_edge(
            quiet, orders, audit, source_column="*", target_column="*",
            transformation_type="TABLE_STAR", review_status="PROPOSED",
        ),
        _trigger_edge(quiet, orders, audit, transformation_type="FILTERED"),
        _trigger_edge(quiet, orders, audit, transformation_type="UNPARSED"),
        _trigger_edge(quiet, orders, audit, is_intermediate=True),
        _trigger_edge(
            quiet, orders, audit, source_column="email", target_column="email",
            review_status="REJECTED",
        ),
    )
    # A routine's gap is the same collector's, and not a trigger's.
    routine = _routine(org, datasource, schema, "load_audit")
    await _add(session, routine)
    await _add(
        session,
        _edge(
            org, datasource, routine,
            source_table="public.orders", target_table="public.orders_audit",
            source_column="*", target_column="*", transformation_type="TABLE_STAR",
            source_table_id=orders.id, target_table_id=audit.id, is_write=True,
        ),
    )
    await session.commit()
    return {
        "org": org,
        "datasource": datasource,
        "schema": schema,
        "orders": orders,
        "audit": audit,
        "triggers": {
            "star_and_ghost": star_and_ghost,
            "unbound": unbound,
            "dropped": dropped,
            "clean": clean,
            "quiet": quiet,
        },
    }


async def _what_propagation_returns(estate: dict[str, Any], session: AsyncSession) -> Any:
    inputs = await collect_propagation_inputs(
        session,
        organization_id=estate["org"].id,
        datasource_id=estate["datasource"].id,
    )
    reasons: dict[str, set[str]] = {}
    for gap in inputs.gaps:
        if gap.edge_source == "TRIGGER_DEFINITION":
            reasons.setdefault(gap.owner_ref, set()).add(gap.reason)
    return inputs, reasons


# ---------------------------------------------------------------------------
# The count and the list are what propagation returns
# ---------------------------------------------------------------------------


async def test_the_register_counts_the_triggers_propagation_reports_gaps_for(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    inputs, returned = await _what_propagation_returns(estate, session)
    triggers = estate["triggers"]
    assert {
        str(triggers["star_and_ghost"].id): {GAP_TABLE_STAR, GAP_COLUMN_NOT_IN_CATALOG},
        str(triggers["unbound"].id): {GAP_SOURCE_UNRESOLVED},
        str(triggers["dropped"].id): {GAP_TABLE_STAR},
    } == returned, "control: this is the estate the test believes it built"
    assert len([g for g in inputs.gaps if g.edge_source == "TRIGGER_DEFINITION"]) == 4

    result = await _register(session, estate["org"])

    count = _kinds(result, estate["datasource"].id)[KIND]
    assert count == len(returned) == 3, "one per trigger, not per gapped edge"
    assert result.totals[KIND] == 3
    (listed,) = result.datasources
    [gap] = [item for item in listed.gaps if item.kind == KIND]
    assert (gap.resolution, gap.owner, gap.explanation) == GAP_DEFINITIONS[KIND]


async def test_the_detail_view_lists_exactly_those_triggers_with_their_reasons(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    _, returned = await _what_propagation_returns(estate, session)

    detail = await footprint_gap_objects(
        session,
        organization_id=estate["org"].id,
        datasource_id=estate["datasource"].id,
        kind=KIND,
    )

    assert {str(item.object_id) for item in detail.objects} == set(returned)
    assert {item.object_type for item in detail.objects} == {"TRIGGER"}
    # Every reason a trigger carries, as the stable codes propagation gives them and nothing else.
    assert {str(item.object_id): item.detail for item in detail.objects} == {
        owner: ",".join(sorted(reasons)) for owner, reasons in returned.items()
    }
    names = {item.qualified_name for item in detail.objects}
    assert any("audit_star on orders" in name for name in names)
    assert (detail.resolution, detail.owner, detail.explanation) == GAP_DEFINITIONS[KIND]
    assert detail.truncated is False and detail.note is None
    # The count and the list agree, which is the point.
    result = await _register(session, estate["org"])
    assert len(detail.objects) == _kinds(result, estate["datasource"].id)[KIND]


async def test_a_since_dropped_trigger_is_still_counted_and_listed(session: AsyncSession) -> None:
    """Propagation does not consult the trigger's own status -- what it copied is still in the
    table it wrote -- so the register must not either, or count and list would each drop it."""
    estate = await _estate(session)
    dropped = estate["triggers"]["dropped"]
    assert dropped.status == "DEPRECATED"

    detail = await footprint_gap_objects(
        session,
        organization_id=estate["org"].id,
        datasource_id=estate["datasource"].id,
        kind=KIND,
    )

    assert dropped.id in {item.object_id for item in detail.objects}


async def test_a_column_that_differs_only_by_case_is_a_reason_too(session: AsyncSession) -> None:
    """The fourth reason propagation names, in the same list as the other three."""
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    orders = await seed_table(session, org, datasource, schema, name="orders")
    audit = await seed_table(session, org, datasource, schema, name="orders_audit")
    await _column(session, orders, "email", classification="PII")
    await _column(session, audit, "Email", ordinal=1)
    await _column(session, audit, "EMAIL", ordinal=2)
    trigger = _trigger(org, datasource, schema, name="audit_case")
    await _add(session, trigger)
    await _add(
        session,
        _trigger_edge(trigger, orders, audit, source_column="email", target_column="email"),
    )
    await session.commit()

    detail = await footprint_gap_objects(
        session, organization_id=org.id, datasource_id=datasource.id, kind=KIND
    )

    assert [(item.object_id, item.detail) for item in detail.objects] == [
        (trigger.id, GAP_COLUMN_AMBIGUOUS)
    ]


# ---------------------------------------------------------------------------
# Nothing else is counted
# ---------------------------------------------------------------------------


async def test_a_routines_gap_is_not_a_triggers(session: AsyncSession) -> None:
    """The same collector reports both. The estate holds a routine gap too, and the trigger
    kind's count and list have none of it."""
    estate = await _estate(session)
    inputs, returned = await _what_propagation_returns(estate, session)
    assert [g for g in inputs.gaps if g.edge_source == "PROCEDURE_DEFINITION"], "control"

    detail = await footprint_gap_objects(
        session,
        organization_id=estate["org"].id,
        datasource_id=estate["datasource"].id,
        kind=KIND,
    )

    assert {str(item.object_id) for item in detail.objects} == set(returned)


async def test_another_source_and_another_organization_are_not_mixed_in(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    org = estate["org"]
    # Another source of the same organization, with a gap of its own.
    _o, elsewhere, elsewhere_schema = await seed_estate(session, organization=org, dialect="tsql")
    other_orders = await seed_table(session, org, elsewhere, elsewhere_schema, name="orders")
    other_audit = await seed_table(session, org, elsewhere, elsewhere_schema, name="orders_audit")
    other = _trigger(org, elsewhere, elsewhere_schema, name="other_star")
    await _add(session, other)
    await _add(
        session,
        _trigger_edge(
            other, other_orders, other_audit, source_column="*", target_column="*",
            transformation_type="TABLE_STAR",
        ),
    )
    # Another organization, with a gap of its own.
    foreign_org, foreign_source, foreign_schema = await seed_estate(session, dialect="tsql")
    foreign_orders = await seed_table(
        session, foreign_org, foreign_source, foreign_schema, name="o"
    )
    foreign_audit = await seed_table(
        session, foreign_org, foreign_source, foreign_schema, name="a"
    )
    foreign = _trigger(foreign_org, foreign_source, foreign_schema, name="foreign_star")
    await _add(session, foreign)
    await _add(
        session,
        _trigger_edge(
            foreign, foreign_orders, foreign_audit, source_column="*", target_column="*",
            transformation_type="TABLE_STAR",
        ),
    )
    await session.commit()

    result = await _register(session, org)

    assert _kinds(result, estate["datasource"].id)[KIND] == 3
    assert _kinds(result, elsewhere.id)[KIND] == 1
    assert result.totals[KIND] == 4, "this organization's two sources, and no one else's"
    ours = await footprint_gap_objects(
        session, organization_id=org.id, datasource_id=estate["datasource"].id, kind=KIND
    )
    assert other.id not in {item.object_id for item in ours.objects}
    assert foreign.id not in {item.object_id for item in ours.objects}
    theirs_asked_with_our_org = await footprint_gap_objects(
        session, organization_id=org.id, datasource_id=foreign_source.id, kind=KIND
    )
    assert theirs_asked_with_our_org.objects == [], "a source is read under its own organization"


async def test_a_source_the_caller_may_not_read_contributes_nothing_to_the_register(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = await _estate(session)
    org = estate["org"]
    _o, denied, denied_schema = await seed_estate(session, organization=org, dialect="tsql")
    denied_orders = await seed_table(session, org, denied, denied_schema, name="orders")
    denied_audit = await seed_table(session, org, denied, denied_schema, name="orders_audit")
    hidden = _trigger(org, denied, denied_schema, name="hidden_star")
    await _add(session, hidden)
    await _add(
        session,
        _trigger_edge(
            hidden, denied_orders, denied_audit, source_column="*", target_column="*",
            transformation_type="TABLE_STAR",
        ),
    )
    await session.commit()
    real_gate = footprint_gaps_module.gate

    async def gate(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("datasource_id") == denied.id:
            raise AuthorizationDenied("DATASOURCE_NOT_GRANTED")
        return await real_gate(*args, **kwargs)

    monkeypatch.setattr(footprint_gaps_module, "gate", gate)

    result = await _register(session, org)

    assert [item.datasource_id for item in result.datasources] == [estate["datasource"].id]
    assert result.totals[KIND] == 3, "not even in the totals"


# ---------------------------------------------------------------------------
# Nothing existing moves
# ---------------------------------------------------------------------------


async def test_an_estate_without_such_a_gap_reads_exactly_as_it_did(session: AsyncSession) -> None:
    """Additive: trigger edges that resolve, and a filter-only one the collector does not read, add
    no kind, no count and no total -- so every existing consumer of the register reads the same."""
    org, datasource, schema = await seed_estate(session, dialect="tsql")
    orders = await seed_table(session, org, datasource, schema, name="orders")
    audit = await seed_table(session, org, datasource, schema, name="orders_audit")
    await session.commit()
    before = await _register(session, org)

    await _column(session, orders, "ssn", classification="PII")
    await _column(session, audit, "ssn")
    trigger = _trigger(org, datasource, schema, name="audit_clean")
    await _add(session, trigger)
    await _add(
        session,
        _trigger_edge(trigger, orders, audit, source_column="ssn", target_column="ssn"),
        _trigger_edge(trigger, orders, audit, transformation_type="FILTERED"),
    )
    await session.commit()
    after = await _register(session, org)

    def shape(result: Any) -> Any:
        return (
            [(d.datasource_id, [(g.kind, g.count) for g in d.gaps]) for d in result.datasources],
            result.totals,
        )

    assert KIND not in _kinds(after, datasource.id)
    assert KIND not in after.totals
    assert shape(after) == shape(before)


async def test_the_response_shape_is_unchanged(session: AsyncSession) -> None:
    """The new kind is a new *value* of `kind`, in the same fields -- no field was added to either
    read model, so the OpenAPI document does not move."""
    estate = await _estate(session)
    result = await _register(session, estate["org"])
    detail = await footprint_gap_objects(
        session,
        organization_id=estate["org"].id,
        datasource_id=estate["datasource"].id,
        kind=KIND,
    )

    assert set(result.model_dump()) == {"organization_id", "generated_at", "datasources", "totals"}
    (listed,) = result.datasources
    assert set(listed.model_dump()) == {
        "datasource_id",
        "datasource_name",
        "gaps",
        "oldest_pending_signal_minutes",
    }
    assert set(listed.gaps[0].model_dump()) == {
        "kind",
        "count",
        "resolution",
        "owner",
        "explanation",
    }
    assert set(detail.model_dump()) == {
        "datasource_id",
        "kind",
        "resolution",
        "owner",
        "explanation",
        "objects",
        "truncated",
        "note",
    }
    assert set(detail.objects[0].model_dump()) == {
        "object_type",
        "object_id",
        "qualified_name",
        "detail",
    }


async def test_the_collector_runs_only_for_a_source_with_reviewed_trigger_edges(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The register is read on every Operations page load and by the metrics sweep. A source
    with no ACTIVE trigger edge cannot have a trigger gap, so it does not pay for the sweep."""
    estate = await _estate(session)
    org = estate["org"]
    _o, quiet_source, quiet_schema = await seed_estate(session, organization=org, dialect="tsql")
    await seed_table(session, org, quiet_source, quiet_schema, name="orders")
    # A source whose only trigger edge is one nobody has decided: propagation reads nothing of it.
    _o, undecided, undecided_schema = await seed_estate(session, organization=org, dialect="tsql")
    undecided_orders = await seed_table(session, org, undecided, undecided_schema, name="orders")
    undecided_audit = await seed_table(
        session, org, undecided, undecided_schema, name="orders_audit"
    )
    proposed = _trigger(org, undecided, undecided_schema, name="proposed_star")
    await _add(session, proposed)
    await _add(
        session,
        _trigger_edge(
            proposed, undecided_orders, undecided_audit, source_column="*", target_column="*",
            transformation_type="TABLE_STAR", review_status="PROPOSED",
        ),
    )
    await session.commit()
    swept: list[UUID] = []
    real = footprint_gaps_module.collect_propagation_inputs

    async def recording(*args: Any, **kwargs: Any) -> Any:
        swept.append(kwargs["datasource_id"])
        return await real(*args, **kwargs)

    monkeypatch.setattr(footprint_gaps_module, "collect_propagation_inputs", recording)

    await _register(session, org)

    assert swept == [estate["datasource"].id]
