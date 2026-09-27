"""ADR-0017 applied to GL-9: a table draft names only same-source lineage.

A description is text everyone who can read the table reads. Naming a table
from another datasource in it would disclose that table past the per-read
cross-source grant check -- the rule the column drafter already follows. An
edge a reviewer rejected (ADR-0026) is not evidence of anything either. Before
this, the steward agent reached both gaps with no human in the loop.

Since 2026-09-11 a draft also cites ADR-0026's parsed lineage -- a view's
definition, pasted procedure SQL, a captured routine -- on the same terms: only
approved edges within the table's own datasource.
"""

from typing import Any
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import compose_draft_text, gather_evidence
from aida.envelope_models import MetadataRoutine
from aida.models import (
    DataSource,
    MetadataTable,
    OpenLineageTableEdge,
    Organization,
    ProcedureLineageEdge,
    ViewLineageEdge,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from tests.support.task_agents import seed_estate, seed_table, task_agent_session


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


def _edge(
    org: Organization,
    source: MetadataTable,
    target: MetadataTable,
    *,
    review_status: str = "ACTIVE",
) -> OpenLineageTableEdge:
    return OpenLineageTableEdge(
        organization_id=org.id,
        # SQLite enforces no foreign keys, and the run event is not under test.
        run_event_id=uuid4(),
        input_dataset_namespace="ns",
        input_dataset_name=source.name,
        input_table_id=source.id,
        output_dataset_namespace="ns",
        output_dataset_name=target.name,
        output_table_id=target.id,
        review_status=review_status,
    )


async def test_a_draft_names_only_same_source_lineage_a_reviewer_kept(
    session: AsyncSession,
) -> None:
    org, warehouse, schema = await seed_estate(session)
    _org, crm, crm_schema = await seed_estate(session, organization=org)
    orders = await seed_table(session, org, warehouse, schema, name="orders")
    raw_orders = await seed_table(session, org, warehouse, schema, name="raw_orders")
    reporting = await seed_table(session, org, warehouse, schema, name="order_reporting")
    abandoned = await seed_table(session, org, warehouse, schema, name="abandoned_feed")
    crm_customers = await seed_table(session, org, crm, crm_schema, name="crm_customers")
    session.add_all(
        [
            _edge(org, raw_orders, orders),
            _edge(org, crm_customers, orders),
            _edge(org, abandoned, orders, review_status="REJECTED"),
            _edge(org, orders, reporting),
        ]
    )
    await session.flush()

    evidence = await gather_evidence(session, orders)

    assert evidence.upstream_table_names == ("raw_orders",)
    assert evidence.downstream_table_names == ("order_reporting",)
    assert evidence.lineage_edge_count == 2
    text = compose_draft_text(evidence)
    assert "crm_customers" not in text
    assert "abandoned_feed" not in text


def _view_edge(
    org: Organization,
    datasource: DataSource,
    source: MetadataTable,
    target: MetadataTable,
    column: str,
    *,
    review_status: str = "ACTIVE",
) -> ViewLineageEdge:
    return ViewLineageEdge(
        organization_id=org.id,
        datasource_id=datasource.id,
        source_table=f"public.{source.name}",
        source_column=column,
        target_table=f"public.{target.name}",
        target_column=column,
        source_table_id=source.id,
        target_table_id=target.id,
        transformation_type="DIRECT",
        confidence="FULL",
        dialect="postgres",
        sql_hash="h" * 64,
        review_status=review_status,
    )


def _procedure_edge(
    org: Organization,
    datasource: DataSource,
    source: MetadataTable,
    target: MetadataTable,
    *,
    review_status: str = "ACTIVE",
) -> ProcedureLineageEdge:
    return ProcedureLineageEdge(
        organization_id=org.id,
        datasource_id=datasource.id,
        source_table=f"public.{source.name}",
        source_column="id",
        target_table=f"public.{target.name}",
        target_column="id",
        source_table_id=source.id,
        target_table_id=target.id,
        transformation_type="DIRECT",
        confidence="FULL",
        dialect="postgres",
        sql_hash="h" * 64,
        review_status=review_status,
    )


def _routine_edge(
    org: Organization,
    datasource: DataSource,
    routine: MetadataRoutine,
    source: MetadataTable,
    target: MetadataTable,
    *,
    is_intermediate: bool = False,
) -> DeepProcedureLineageEdge:
    return DeepProcedureLineageEdge(
        organization_id=org.id,
        datasource_id=datasource.id,
        routine_id=routine.id,
        statement_ordinal=0,
        source_table=f"public.{source.name}",
        source_column="amount",
        target_table=f"public.{target.name}",
        target_column="total",
        source_table_id=source.id,
        target_table_id=target.id,
        transformation_type="DIRECT",
        confidence="FULL",
        dialect="tsql",
        is_write=True,
        is_intermediate=is_intermediate,
        sql_hash="h" * 64,
        review_status="ACTIVE",
        created_by="reviewer-1",
    )


async def test_a_draft_cites_approved_parsed_lineage_and_nothing_still_under_review(
    session: AsyncSession,
) -> None:
    """Parsed lineage is evidence on OpenLineage's terms. An approved edge within
    the datasource is cited, once per neighbouring table. A PROPOSED edge --
    every edge the lineage agent writes, until a person approves it -- and a
    REJECTED one are not, and neither is a procedure's hop into a temp table."""
    org, warehouse, schema = await seed_estate(session)
    orders = await seed_table(session, org, warehouse, schema, name="orders")
    customers = await seed_table(session, org, warehouse, schema, name="customers")
    pending = await seed_table(session, org, warehouse, schema, name="pending_source")
    rejected = await seed_table(session, org, warehouse, schema, name="rejected_source")
    totals = await seed_table(session, org, warehouse, schema, name="order_totals")
    stage = await seed_table(session, org, warehouse, schema, name="stage")
    routine = MetadataRoutine(
        organization_id=org.id,
        datasource_id=warehouse.id,
        schema_id=schema.id,
        name="load_totals",
        routine_type="PROCEDURE",
        body_sql_redacted="-- redacted body",
        fingerprint="fp",
    )
    session.add(routine)
    await session.flush()
    session.add_all(
        [
            # Two column pairs from one view: one neighbour, cited once.
            _view_edge(org, warehouse, customers, orders, "customer_id"),
            _view_edge(org, warehouse, customers, orders, "customer_name"),
            _view_edge(org, warehouse, pending, orders, "id", review_status="PROPOSED"),
            _procedure_edge(org, warehouse, rejected, orders, review_status="REJECTED"),
            _routine_edge(org, warehouse, routine, orders, totals),
            _routine_edge(org, warehouse, routine, orders, stage, is_intermediate=True),
        ]
    )
    await session.flush()

    evidence = await gather_evidence(session, orders)

    assert evidence.upstream_table_names == ("customers",)
    assert evidence.downstream_table_names == ("order_totals",)
    assert [edge_type for edge_type, _ in evidence.upstream_parsed_edges] == ["VIEW"]
    assert [edge_type for edge_type, _ in evidence.downstream_parsed_edges] == ["ROUTINE"]
    assert evidence.lineage_edge_count == 2
    text = compose_draft_text(evidence)
    assert "populated from customers" in text
    assert "feeds order_totals" in text
    for absent in ("pending_source", "rejected_source", "stage"):
        assert absent not in text
