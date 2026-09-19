"""R11-FP05: uncertain grain is a counted, routed gap.

A table or view whose row grain the platform cannot establish -- no key, an undecided choice of
key, a view that aggregates without its grouping among its outputs -- is what makes an answer
double-count, and the gap register counted everything FP05 named except that. These tests pin
the derivation, which uses only evidence the platform already holds:

* grain is established by a declared primary key or unique constraint, a unique or primary
  index, or a composite-key candidate a steward APPROVED -- and by nothing weaker: a PENDING
  candidate is the question, not the answer;
* each counted object expands, in the drill-down, into the code for the step that closes it;
* a datasource the caller may not read contributes to no count.

Fails on the tree before R11-FP05's grain slice: `GRAIN_UNCERTAIN` was not a kind at all, so the
count, the route and the drill-down (`UnknownGapKind`) were all missing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

import aida.footprint_gaps as footprint_gaps_module
from aida.authorization_gate import AuthorizationDenied
from aida.footprint_gap_detail import (
    GRAIN_AGGREGATE_WITHOUT_GROUPING,
    GRAIN_AMBIGUOUS_KEY,
    GRAIN_KEY_UNCONFIRMED,
    GRAIN_KEYS_NOT_READ,
    GRAIN_NO_KEY,
    footprint_gap_objects,
)
from aida.footprint_gaps import GAP_DEFINITIONS, footprint_gaps
from aida.models import (
    AnalysisRun,
    CompositeKeyCandidate,
    MetadataConstraint,
    MetadataIndex,
    ViewLineageEdge,
)
from tests.support.task_agents import (
    agent_settings,
    human,
    seed_estate,
    seed_table,
    task_agent_session,
)

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


def _constraint(org: Any, datasource: Any, table: Any, kind: str) -> MetadataConstraint:
    return MetadataConstraint(
        organization_id=org.id,
        datasource_id=datasource.id,
        table_id=table.id,
        name=f"{kind.lower()}_{uuid4().hex[:6]}",
        constraint_type=kind,
        columns=["id"],
        status="ACTIVE",
        fingerprint="fp",
    )


def _candidate(org: Any, datasource: Any, table: Any, status: str) -> CompositeKeyCandidate:
    return CompositeKeyCandidate(
        organization_id=org.id,
        datasource_id=datasource.id,
        table_id=table.id,
        column_ids=[str(uuid4())],
        column_names=["id"],
        column_count=1,
        key_fingerprint=uuid4().hex,
        detection_rule="PROFILE_DISTINCT",
        confidence=0.6,
        estimated_distinctness_ratio=1.0,
        evidence={},
        status=status,
        created_by="profiler",
    )


def _view_edge(org: Any, datasource: Any, view: Any, kind: str, column: str) -> ViewLineageEdge:
    return ViewLineageEdge(
        organization_id=org.id,
        datasource_id=datasource.id,
        source_table="public.orders",
        source_column="amount" if kind == "AGGREGATED" else "region",
        target_table=f"public.{view.name}",
        target_column=column,
        target_table_id=view.id,
        transformation_type=kind,
        confidence="FULL",
        dialect="postgres",
        sql_hash=uuid4().hex,
        review_status="ACTIVE",
    )


async def _estate(session: AsyncSession) -> dict[str, Any]:
    org, datasource, schema = await seed_estate(session)
    _, denied, denied_schema = await seed_estate(session, organization=org)

    async def table(name: str, object_type: str = "BASE_TABLE") -> Any:
        return await seed_table(session, org, datasource, schema, name=name,
                                object_type=object_type)

    objects = {
        # Established: each by one kind of evidence.
        "with_pk": await table("with_pk"),
        "with_unique": await table("with_unique"),
        "with_unique_index": await table("with_unique_index"),
        "with_approved_key": await table("with_approved_key"),
        # Uncertain, one per code.
        "keyless": await table("keyless"),
        "one_candidate": await table("one_candidate"),
        "two_candidates": await table("two_candidates"),
        "rejected_only": await table("rejected_only"),
        "v_totals": await table("v_totals", "VIEW"),
        "v_by_region": await table("v_by_region", "VIEW"),
        # Not ACTIVE: not in the catalog's present, so in no count.
        "retired": await table("retired"),
    }
    objects["retired"].status = "DEPRECATED"
    denied_table = await seed_table(session, org, denied, denied_schema, name="elsewhere")
    session.add_all(
        [
            _constraint(org, datasource, objects["with_pk"], "PRIMARY_KEY"),
            _constraint(org, datasource, objects["with_unique"], "UNIQUE"),
            # A foreign key states nothing about this table's own grain.
            _constraint(org, datasource, objects["keyless"], "FOREIGN_KEY"),
            MetadataIndex(
                organization_id=org.id,
                datasource_id=datasource.id,
                table_id=objects["with_unique_index"].id,
                name="ux_code",
                index_type="BTREE",
                columns=["code"],
                is_unique=True,
                is_primary=False,
                status="ACTIVE",
                fingerprint="fp",
            ),
            _candidate(org, datasource, objects["with_approved_key"], "APPROVED"),
            _candidate(org, datasource, objects["one_candidate"], "PENDING"),
            _candidate(org, datasource, objects["two_candidates"], "PENDING"),
            _candidate(org, datasource, objects["two_candidates"], "PENDING"),
            _candidate(org, datasource, objects["rejected_only"], "REJECTED"),
            # SELECT SUM(amount) AS total FROM orders: aggregated, nothing passed through.
            _view_edge(org, datasource, objects["v_totals"], "AGGREGATED", "total"),
            # SELECT region, SUM(amount) ... GROUP BY region: a grouping column is an output,
            # but no key is declared or approved, so its grain is still not established.
            _view_edge(org, datasource, objects["v_by_region"], "AGGREGATED", "total"),
            _view_edge(org, datasource, objects["v_by_region"], "DIRECT", "region"),
        ]
    )
    await session.commit()
    return {"org": org, "datasource": datasource, "denied": denied,
            "denied_table": denied_table, **objects}


async def _counted(session: AsyncSession, estate: dict[str, Any], monkeypatch) -> Any:
    real_gate = footprint_gaps_module.gate

    async def gate(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("datasource_id") == estate["denied"].id:
            raise AuthorizationDenied("DATASOURCE_NOT_GRANTED")
        return await real_gate(*args, **kwargs)

    monkeypatch.setattr(footprint_gaps_module, "gate", gate)
    return await footprint_gaps(
        session,
        context=human(estate["org"], "ops-1", frozenset({"Operations"})),
        settings=agent_settings(),
        organization_id=estate["org"].id,
        now=NOW,
    )


async def test_uncertain_grain_is_counted_from_evidence_already_held(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    estate = await _estate(session)
    result = await _counted(session, estate, monkeypatch)

    (listed,) = result.datasources
    [gap] = [item for item in listed.gaps if item.kind == "GRAIN_UNCERTAIN"]
    # keyless, one_candidate, two_candidates, rejected_only, v_totals, v_by_region.
    assert gap.count == 6
    assert (gap.resolution, gap.owner, gap.explanation) == GAP_DEFINITIONS["GRAIN_UNCERTAIN"]
    assert gap.resolution == "HUMAN_REVIEW"
    # The denied source's keyless table is in no count, total included.
    assert result.totals["GRAIN_UNCERTAIN"] == 6


async def test_each_uncertain_object_names_the_step_that_closes_it(
    session: AsyncSession,
) -> None:
    estate = await _estate(session)
    detail = await footprint_gap_objects(
        session,
        organization_id=estate["org"].id,
        datasource_id=estate["datasource"].id,
        kind="GRAIN_UNCERTAIN",
    )
    codes = {item.qualified_name.rsplit(".", 1)[1]: item.detail for item in detail.objects}
    assert codes == {
        "keyless": GRAIN_NO_KEY,
        "one_candidate": GRAIN_KEY_UNCONFIRMED,
        "two_candidates": GRAIN_AMBIGUOUS_KEY,
        # A steward said no; the grain is as unknown as it was.
        "rejected_only": GRAIN_NO_KEY,
        "v_totals": GRAIN_AGGREGATE_WITHOUT_GROUPING,
        "v_by_region": GRAIN_NO_KEY,
    }
    kinds = {item.qualified_name.rsplit(".", 1)[1]: item.object_type for item in detail.objects}
    assert kinds["v_totals"] == "VIEW" and kinds["keyless"] == "TABLE"
    assert detail.truncated is False
    # Identity and a code: nothing a source wrote, no definition, no value.
    for item in detail.objects:
        assert item.detail is not None and item.detail.isupper()
    assert all(
        estate["denied_table"].id != item.object_id for item in detail.objects
    )


async def test_a_run_that_did_not_read_constraints_says_so_instead_of_no_key(
    session: AsyncSession,
) -> None:
    """A refused constraints read returns nothing, so "no key" would be a claim about a table
    Atlas never looked at. The last completed run's receipt decides it."""
    estate = await _estate(session)
    for at, state in ((NOW - timedelta(days=1), "SUPPORTED"), (NOW, "PERMISSION_DENIED")):
        session.add(
            AnalysisRun(
                id=uuid4(),
                organization_id=estate["org"].id,
                datasource_id=estate["datasource"].id,
                mode="FULL",
                status="COMPLETED",
                created_at=at,
                updated_at=at,
                discovery_receipt={
                    "facets": {"constraints": {"support": "SUPPORTED", "state": state}}
                },
            )
        )
    await session.commit()

    detail = await footprint_gap_objects(
        session,
        organization_id=estate["org"].id,
        datasource_id=estate["datasource"].id,
        kind="GRAIN_UNCERTAIN",
    )
    codes = {item.qualified_name.rsplit(".", 1)[1]: item.detail for item in detail.objects}
    assert codes["keyless"] == GRAIN_KEYS_NOT_READ
    # A pending decision is still the next step where there is one.
    assert codes["one_candidate"] == GRAIN_KEY_UNCONFIRMED
