"""ADR-0029: the steward agent's column descriptions.

* It drafts the columns of the worklist's tables that have no approved
  description, were not retired and have no draft open -- from catalog
  evidence, never a model -- and puts each in review as its own request.
* A column too thin for the review bar is passed over without an item, and
  text a reviewer rejected for a column is never raised again.
* A person publishes the draft; the agent cannot.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import aida.semantic_api  # noqa: F401 -- registers the decision target adapters
from aida.column_documentation import (
    current_descriptions_by_column_id,
    publish_column_description,
)
from aida.governance_decision_service import GovernanceDecisionRefused, decide_review
from aida.models import (
    AgentTask,
    ColumnDescriptionDraft,
    GovernanceReview,
    MetadataColumn,
    MetadataTable,
    Organization,
)
from aida.security import SecurityContext
from aida.steward_agent import (
    CAPABILITY_COLUMN_DESCRIPTION,
    SKIP_OPEN_DRAFT,
    SKIP_REJECTED_BEFORE,
    run_steward_agent,
)
from aida.task_agent import (
    ACTION_PROPOSED,
    ACTION_SKIPPED,
    ACTION_WOULD_PROPOSE,
    TaskAgentOutcome,
    TaskAgentRunRequest,
)
from tests.support.task_agents import (
    agent_settings,
    count_rows,
    human,
    register_agent,
    seed_estate,
    seed_table,
    task_agent_session,
)

AGENT = "agent:steward"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


async def _column(
    session: AsyncSession,
    org: Organization,
    table: MetadataTable,
    *,
    name: str,
    position: int,
    comment: str | None = None,
) -> MetadataColumn:
    column = MetadataColumn(
        organization_id=org.id,
        table_id=table.id,
        name=name,
        ordinal_position=position,
        physical_type="text",
        nullable=True,
        fingerprint="fp",
        source_description=comment,
    )
    session.add(column)
    await session.flush()
    return column


async def _estate(
    session: AsyncSession,
) -> tuple[Organization, MetadataTable, dict[str, MetadataColumn]]:
    """An undocumented worklist table: `customer_id` carries a source comment
    (enough evidence to submit), `notes` carries nothing (too thin), and
    `segment` already has an approved description."""
    org, datasource, schema = await seed_estate(session)
    table = await seed_table(session, org, datasource, schema, name="customers")
    columns = {
        "customer_id": await _column(
            session,
            org,
            table,
            name="customer_id",
            position=1,
            comment="The customer's identifier in the core banking system.",
        ),
        "notes": await _column(session, org, table, name="notes", position=2),
        "segment": await _column(
            session, org, table, name="segment", position=3, comment="Marketing segment."
        ),
    }
    await publish_column_description(
        session,
        organization_id=org.id,
        table_id=table.id,
        column_id=columns["segment"].id,
        description="The marketing segment the CRM assigns.",
        created_by="steward-2",
        approved_by="steward-3",
        approved_at=NOW,
    )
    await session.flush()
    return org, table, columns


async def _run(session: AsyncSession, org: Organization) -> TaskAgentOutcome:
    return await run_steward_agent(
        session,
        org.id,
        request=TaskAgentRunRequest(capabilities=(CAPABILITY_COLUMN_DESCRIPTION,)),
        settings=agent_settings(),
        triggered_by=human(org),
    )


async def test_an_undescribed_column_with_evidence_is_the_agents_request(
    session: AsyncSession,
) -> None:
    org, _table, columns = await _estate(session)
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    [item] = outcome.items
    assert (item.capability, item.action, item.subject_name) == (
        CAPABILITY_COLUMN_DESCRIPTION,
        ACTION_PROPOSED,
        "customers.customer_id",
    )
    draft = (await session.scalars(select(ColumnDescriptionDraft))).one()
    assert (draft.column_id, draft.status, draft.created_by) == (
        columns["customer_id"].id,
        "PENDING_APPROVAL",
        AGENT,
    )
    assert draft.evidence["origin"] == "METADATA"
    review = (await session.scalars(select(GovernanceReview))).one()
    assert (review.object_type, review.object_id, review.requested_by) == (
        "COLUMN_DESCRIPTION_DRAFT",
        str(draft.id),
        AGENT,
    )
    assert draft.governance_review_id == review.id
    task = (await session.scalars(select(AgentTask))).one()
    assert task.intent == "steward.propose_column_description"


async def test_a_t0_contract_only_previews(session: AsyncSession) -> None:
    org, _table, _columns = await _estate(session)
    await register_agent(session, org, principal=AGENT, tier="T0")

    outcome = await _run(session, org)

    assert [item.action for item in outcome.items] == [ACTION_WOULD_PROPOSE]
    assert await count_rows(session, ColumnDescriptionDraft) == 0


async def test_a_column_with_a_draft_open_is_left_alone(session: AsyncSession) -> None:
    org, table, columns = await _estate(session)
    session.add(
        ColumnDescriptionDraft(
            organization_id=org.id,
            table_id=table.id,
            column_id=columns["customer_id"].id,
            drafted_text="A steward's draft in progress.",
            text_fingerprint="f" * 64,
            accuracy_score=0.5,
            clarity_score=0.5,
            style_score=0.5,
            completeness_score=0.5,
            overall_score=0.5,
            evidence={},
            status="DRAFT",
            created_by="steward-2",
        )
    )
    await session.flush()
    await register_agent(session, org, principal=AGENT)

    outcome = await _run(session, org)

    assert [(item.action, item.reason) for item in outcome.items] == [
        (ACTION_SKIPPED, SKIP_OPEN_DRAFT)
    ]
    assert await count_rows(session, GovernanceReview) == 0


async def test_a_person_publishes_the_draft_and_the_agent_cannot(
    session: AsyncSession,
) -> None:
    org, _table, columns = await _estate(session)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org)
    review = (await session.scalars(select(GovernanceReview))).one()
    agent_context = SecurityContext(
        principal_id=AGENT,
        principal_type="AGENT",
        organization_id=org.id,
        roles=frozenset({"Reviewer"}),
    )

    with pytest.raises(GovernanceDecisionRefused):
        await decide_review(
            session, review, decision="APPROVE", reason="self", context=agent_context, now=NOW
        )
    await decide_review(
        session,
        review,
        decision="APPROVE",
        reason="matches the source comment",
        context=human(org, "reviewer-1"),
        now=NOW,
    )

    draft = (await session.scalars(select(ColumnDescriptionDraft))).one()
    assert draft.status == "APPROVED"
    described = await current_descriptions_by_column_id(session, [columns["customer_id"].id])
    assert columns["customer_id"].id in described


async def test_text_a_reviewer_rejected_is_never_raised_again(session: AsyncSession) -> None:
    org, _table, _columns = await _estate(session)
    await register_agent(session, org, principal=AGENT)
    await _run(session, org)
    review = (await session.scalars(select(GovernanceReview))).one()
    await decide_review(
        session,
        review,
        decision="REJECT",
        reason="too generic",
        context=human(org, "reviewer-1"),
        now=NOW,
    )

    again = await _run(session, org)

    assert [(item.action, item.reason) for item in again.items] == [
        (ACTION_SKIPPED, SKIP_REJECTED_BEFORE)
    ]
    assert await count_rows(session, ColumnDescriptionDraft) == 1
