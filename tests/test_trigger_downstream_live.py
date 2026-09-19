"""R11-FP01, downstream, on real engines: a real trigger's reviewed lineage is read.

`tests/test_trigger_downstream.py` proves each reader against seeded rows. This is
the same claim with nothing seeded but a classification: a real trigger that
copies `orders.customer_id` into a second table on live PostgreSQL and SQL Server,
scanned by the real connector, parsed by the real lineage agent, approved by a
person through the one decision endpoint -- and then, per engine:

* **classification propagation** carries a PII label on `orders.customer_id` to the
  written table's `customer_id`, and did not while the edge was only a proposal;
* **description drafting** names the trigger as what writes the table, and did not
  while the edge was only a proposal;
* **`get_transformation_detail`**: on SQL Server the trigger's own body comes back
  through the routine gate, and the graph edge references it; on PostgreSQL the
  trigger reports no body and points at the function that carries it;
* **retrieval** (PostgreSQL): a question naming only the trigger function reaches
  the written table through the graph stage. On SQL Server a trigger has no routine
  to ride on, and retrieval has no trigger candidate -- that half is a declared gap,
  not asserted here.

Reuses the footprint journey's private source and `test_trigger_lineage_live`'s
trigger, scan and agent helpers, so it creates no server of its own and skips with
the journey when one is unreachable. Every object it creates is dropped in a
`finally`, and the journey fixture drops the whole database behind it.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aida.asset_description_service import compose_draft_text, gather_evidence
from aida.classification_propagation import propagate_for_datasource
from aida.config import Settings
from aida.envelope_models import MetadataTrigger
from aida.mcp_server import _transformation_detail
from aida.models import MetadataColumn, MetadataTable, Organization
from aida.parsed_lineage_review_api import decide_parsed_lineage_edge
from aida.procedure_lineage_models import TriggerLineageEdge
from aida.retrieval import hybrid_retrieve_enhanced
from aida.schemas import ParsedLineageEdgeDecisionRequest
from aida.task_agent import ACTION_PROPOSED
from aida.unified_lineage_api import build_unified_lineage_graph_payload
from tests.support.task_agents import human, register_agent
from tests.test_footprint_journey import (  # noqa: F401 -- fixtures are used by name
    JourneySource,
    _postgres,
    _sqlserver,
    source,
)
from tests.test_trigger_lineage_live import (  # noqa: F401 -- `platform` is a fixture
    _CREATE,
    _TEARDOWN,
    AGENT,
    AUDIT,
    FUNCTION,
    TRIGGER,
    _datasource,
    _run_agent,
    _scan,
    _table_id,
    platform,
)


async def _customer_id_column(
    session: AsyncSession, organization_id: UUID, table_id: object
) -> MetadataColumn:
    column = await session.scalar(
        select(MetadataColumn).where(
            MetadataColumn.organization_id == organization_id,
            MetadataColumn.table_id == table_id,
            MetadataColumn.name == "customer_id",
        )
    )
    assert column is not None, "the scan created no customer_id column"
    return column


async def test_a_real_triggers_reviewed_lineage_reaches_every_downstream_reader(
    platform: AsyncSession,  # noqa: F811 -- the imported fixture, used by name
    source: JourneySource,  # noqa: F811 -- the journey's fixture, used by name
) -> None:
    datasource = await _datasource(platform, source)
    organization = await platform.get(Organization, datasource.organization_id)
    assert organization is not None
    try:
        await source.execute(_CREATE[source.connector_type])
        await _scan(platform, datasource, source)
        await register_agent(platform, organization, principal=AGENT)
        trigger = await platform.scalar(
            select(MetadataTrigger).where(
                MetadataTrigger.organization_id == datasource.organization_id,
                MetadataTrigger.datasource_id == datasource.id,
                MetadataTrigger.name == TRIGGER,
            )
        )
        assert trigger is not None
        orders_id = await _table_id(platform, datasource, "orders")
        audit_id = await _table_id(platform, datasource, AUDIT)
        audit = await platform.get(MetadataTable, audit_id)
        assert audit is not None

        # A steward's PII label on the firing table's column -- the only thing
        # this test writes into the catalog by hand.
        origin = await _customer_id_column(platform, organization.id, orders_id)
        copy = await _customer_id_column(platform, organization.id, audit_id)
        await platform.execute(
            update(MetadataColumn)
            .where(MetadataColumn.id == origin.id)
            .values(classification="PII")
        )
        await platform.flush()

        # --- PROPOSED: the agent's edge moves nothing downstream -------------
        assert await _run_agent(platform, datasource) == [(ACTION_PROPOSED, trigger.id)]
        [edge] = (
            await platform.scalars(
                select(TriggerLineageEdge).where(
                    TriggerLineageEdge.trigger_id == trigger.id,
                    TriggerLineageEdge.target_table_id == audit_id,
                )
            )
        ).all()
        assert edge.review_status == "PROPOSED"
        assert (
            await propagate_for_datasource(
                platform,
                organization_id=organization.id,
                datasource_id=datasource.id,
                created_by="scheduler",
            )
            == []
        )
        assert (await gather_evidence(platform, audit)).writing_triggers == ()

        # --- a person approves it through the one decision endpoint ----------
        decided = await decide_parsed_lineage_edge(
            edge.id,
            ParsedLineageEdgeDecisionRequest(
                edge_type="TRIGGER", decision="APPROVED", reason="the trigger writes audit"
            ),
            context=human(organization),
            session=platform,
        )
        assert decided.review_status == "ACTIVE"

        # --- classification propagation --------------------------------------
        written = await propagate_for_datasource(
            platform,
            organization_id=organization.id,
            datasource_id=datasource.id,
            created_by="scheduler",
        )
        assert [(row.column_id, row.classification) for row in written] == [(copy.id, "PII")]
        assert written[0].origin_column_id == origin.id
        await platform.refresh(copy)
        assert copy.classification != "PII", "a derived value must never become asserted"

        # --- description drafting --------------------------------------------
        evidence = await gather_evidence(platform, audit)
        assert [name for name, _firing, _id in evidence.writing_triggers] == [TRIGGER]
        text = compose_draft_text(evidence)
        assert f"written by database trigger {TRIGGER} (fires on orders)" in text
        assert "INSERT" not in text

        # --- get_transformation_detail, and the edge that names it -----------
        detail = await _transformation_detail(platform, datasource, trigger.id)
        assert detail is not None and detail["transformation_source"] == "TRIGGER_BODY"
        graph = await build_unified_lineage_graph_payload(platform, datasource, settings=None)
        [folded] = [e for e in graph.edges if e.edge_source == "TRIGGER_DEFINITION"]
        reference = folded.evidence["transformation_reference"]
        if source.connector_type == "sqlserver":
            # The engine's own trigger body, released through the routine gate.
            assert detail["body_withheld_reason"] is None
            assert detail["body_sql_redacted"] is not None
            assert AUDIT in detail["body_sql_redacted"]
            assert reference["kind"] == "TRIGGER_BODY"
            assert reference["entity_id"] == str(trigger.id)
            return

        # PostgreSQL: no trigger body; the function carries it.
        assert detail["body_sql_redacted"] is None
        assert detail["body_withheld_reason"] == "TRIGGER_BODY_UNAVAILABLE"
        assert detail["body_reference"] is not None
        function_id = detail["body_reference"]["entity_id"]
        assert reference == detail["body_reference"]
        function_detail = await _transformation_detail(platform, datasource, UUID(function_id))
        assert function_detail is not None
        assert function_detail["name"].lower() == FUNCTION

        # --- retrieval: a question naming only the function reaches the table --
        hits = await hybrid_retrieve_enhanced(
            platform,
            datasource=datasource,
            question="note",
            settings=Settings(_env_file=None),
            include_vector=False,
        )
        [routine_hit] = [
            hit for hit in hits if hit.object_type == "ROUTINE" and hit.object_id == function_id
        ]
        assert routine_hit.metadata["writes_table_ids"] == [str(audit_id)]
        assert routine_hit.metadata["trigger_ids"] == [str(trigger.id)]
        [audit_hit] = [
            hit for hit in hits if hit.object_type == "TABLE" and hit.object_id == str(audit_id)
        ]
        # Reached through the function's node: the edge its trigger lineage added.
        assert f"ROUTINE:{function_id}" in str(audit_hit.metadata)
    finally:
        await source.execute(_TEARDOWN[source.connector_type])
