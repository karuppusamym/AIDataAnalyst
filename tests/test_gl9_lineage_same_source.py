"""ADR-0017 applied to GL-9: a table draft names only same-source lineage.

A description is text everyone who can read the table reads. Naming a table
from another datasource in it would disclose that table past the per-read
cross-source grant check -- the rule the column drafter already follows. An
edge a reviewer rejected (ADR-0026) is not evidence of anything either. Before
this, the steward agent reached both gaps with no human in the loop.
"""

from typing import Any
from uuid import uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import compose_draft_text, gather_evidence
from aida.models import MetadataTable, OpenLineageTableEdge, Organization
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
