"""ADR-0029: the ingest side-car under the steward agent's contract.

An organization that registered the steward agent has put automated table
drafting under that contract, and the side-car (`newly_created_table_drafter`)
honours it: it drafts as the agent, stops when the agent's kill switch is
engaged or its contract only observes, and submits a reviewable draft as the
agent's own request with a ledger row. An organization that never registered
the agent sees the side-car behave exactly as before -- which is what keeps
this from being the behaviour change the ADR declined.
"""

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contracts import REASON_KILL_ENGAGED
from aida.models import (
    AgentTask,
    AssetDescriptionDraft,
    AuditEvent,
    DataSource,
    DbtResource,
    GovernanceReview,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
)
from aida.newly_created_table_drafter import (
    INTENT_DRAFT_ON_INGEST,
    REASON_STEWARD_OBSERVES_ONLY,
    handle_newly_created_table,
)
from tests.support.task_agents import (
    count_rows,
    register_agent,
    seed_estate,
    seed_table,
    task_agent_session,
)

AGENT = "agent:steward"


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


@pytest.fixture(autouse=True)
def _fresh_settings() -> Iterator[None]:
    from aida.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _table(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    schema: MetadataSchema,
    *,
    evidence: bool,
) -> MetadataTable:
    """With `evidence`, three columns and a dbt description: a GL-9 draft
    scoring about 0.48, over the 0.4 review bar. Without, it scores under it."""
    table = await seed_table(session, org, datasource, schema, name="orders")
    if evidence:
        for position in range(3):
            session.add(
                MetadataColumn(
                    organization_id=org.id,
                    table_id=table.id,
                    name=f"col_{position}",
                    ordinal_position=position + 1,
                    physical_type="text",
                    nullable=True,
                    fingerprint="fp",
                )
            )
        session.add(
            DbtResource(
                organization_id=org.id,
                artifact_import_id=uuid4(),
                unique_id="model.bank.orders",
                resource_type="model",
                package_name="bank",
                name="orders",
                sql_parse_status="PARSED",
                description="Every order a customer placed, one row per order.",
                matched_table_id=table.id,
            )
        )
        await session.flush()
    return table


async def _ingest(
    session: AsyncSession, *, tier: str | None = None, kill_engaged: bool = False,
    evidence: bool = True,
) -> MetadataTable:
    org, datasource, schema = await seed_estate(session)
    if tier is not None:
        await register_agent(session, org, principal=AGENT, tier=tier, kill_engaged=kill_engaged)
    table = await _table(session, org, datasource, schema, evidence=evidence)
    await handle_newly_created_table(
        session,
        {
            "organization_id": str(org.id),
            "datasource_id": str(datasource.id),
            "table_id": str(table.id),
        },
    )
    return table


async def test_an_organization_that_never_registered_the_agent_sees_no_change(
    session: AsyncSession,
) -> None:
    await _ingest(session)

    draft = (await session.scalars(select(AssetDescriptionDraft))).one()
    assert (draft.created_by, draft.status) == ("auto-enqueue-drafter", "DRAFT")
    assert await count_rows(session, GovernanceReview) == 0
    assert await count_rows(session, AgentTask) == 0


async def test_under_the_contract_a_reviewable_draft_is_the_agents_request(
    session: AsyncSession,
) -> None:
    await _ingest(session, tier="T1")

    draft = (await session.scalars(select(AssetDescriptionDraft))).one()
    assert (draft.created_by, draft.status) == (AGENT, "PENDING_APPROVAL")
    review = (await session.scalars(select(GovernanceReview))).one()
    assert (review.object_type, review.object_id, review.requested_by) == (
        "ASSET_DESCRIPTION_DRAFT",
        str(draft.id),
        AGENT,
    )
    assert draft.governance_review_id == review.id
    task = (await session.scalars(select(AgentTask))).one()
    assert (task.intent, task.agent_principal_id) == (INTENT_DRAFT_ON_INGEST, AGENT)
    assert (task.proposal_ref_type, task.proposal_ref_id) == ("GOVERNANCE_REVIEW", review.id)


async def test_under_the_contract_a_thin_draft_stays_a_draft(session: AsyncSession) -> None:
    await _ingest(session, tier="T1", evidence=False)

    draft = (await session.scalars(select(AssetDescriptionDraft))).one()
    assert (draft.created_by, draft.status) == (AGENT, "DRAFT")
    assert await count_rows(session, GovernanceReview) == 0
    task = (await session.scalars(select(AgentTask))).one()
    assert (task.proposal_ref_type, task.proposal_ref_id) == ("ASSET_DESCRIPTION_DRAFT", draft.id)


@pytest.mark.parametrize(
    ("tier", "kill_engaged", "reason"),
    [("T1", True, REASON_KILL_ENGAGED), ("T0", False, REASON_STEWARD_OBSERVES_ONLY)],
    ids=["kill-switch", "observe-only"],
)
async def test_the_contract_stops_the_side_car(
    session: AsyncSession, tier: str, kill_engaged: bool, reason: str
) -> None:
    table = await _ingest(session, tier=tier, kill_engaged=kill_engaged)

    assert await count_rows(session, AssetDescriptionDraft) == 0
    assert await count_rows(session, AgentTask) == 0
    stopped = (
        await session.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "AUTO_ENQUEUE_DRAFTS_ON_INGEST",
                AuditEvent.outcome == "SKIPPED",
            )
        )
    ).one()
    assert (stopped.resource_id, stopped.details["reason"]) == (str(table.id), reason)
