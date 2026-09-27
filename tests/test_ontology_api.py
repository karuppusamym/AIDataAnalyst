"""Governed ontology v1 (bc6ab78), end to end through the review queue.

The module shipped with its router unwired, no migration, no tier for its
review type and no decision adapter, so an ontology version could be created
by nobody and approved by nothing. What is exercised here is the path now
wired: create a draft, submit it, and have it decided through the shared
governance queue -- by someone other than its author -- which publishes it.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import aida.semantic_api  # noqa: F401 -- registers the decision target adapters
from aida.governance_decision_service import GovernanceDecisionRefused, decide_review
from aida.models import GovernanceReview, Organization
from aida.ontology_api import (
    Concept,
    OntologyCreate,
    OntologyDefinition,
    create_ontology_version,
    submit_ontology_version,
)
from aida.ontology_models import OntologyHead, OntologyVersion
from aida.review_risk_tiers import TIER_T2, agent_decidable_object_types, risk_tier_for
from tests.support.task_agents import agent_settings, human, seed_estate, task_agent_session

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
ROLES = frozenset({"DataSteward"})


@pytest_asyncio.fixture
async def session() -> Any:
    async with task_agent_session() as active:
        yield active


def _definition(*concepts: str) -> OntologyDefinition:
    return OntologyDefinition(
        name="Retail banking",
        owner="data-governance",
        provenance="Agreed at the 2026-09 glossary council.",
        concepts=[
            Concept(key=key, name=key.title(), description=f"The {key} concept.")
            for key in concepts
        ],
    )


async def _create(
    session: AsyncSession, org: Organization, *concepts: str, base_version: int = 0
) -> Any:
    return await create_ontology_version(
        org.id,
        OntologyCreate(
            ontology_key="retail", base_version=base_version, definition=_definition(*concepts)
        ),
        context=human(org, "author-1", ROLES),
        session=session,
        settings=agent_settings(),
    )


async def _submit(session: AsyncSession, org: Organization, version_id: Any) -> Any:
    return await submit_ontology_version(
        version_id,
        context=human(org, "author-1", ROLES),
        session=session,
        settings=agent_settings(),
    )


async def test_a_submitted_ontology_is_published_by_a_reviewer_not_its_author(
    session: AsyncSession,
) -> None:
    org, _datasource, _schema = await seed_estate(session)
    await session.commit()
    created = await _create(session, org, "customer", "account")
    submitted = await _submit(session, org, created.id)
    review = await session.get(GovernanceReview, submitted.governance_review_id)
    assert review is not None
    assert (review.object_type, review.requested_by) == ("ONTOLOGY_VERSION", "author-1")

    with pytest.raises(GovernanceDecisionRefused):
        await decide_review(
            session,
            review,
            decision="APPROVE",
            reason="mine",
            context=human(org, "author-1", ROLES),
            now=NOW,
        )
    await decide_review(
        session,
        review,
        decision="APPROVE",
        reason="agreed wording",
        context=human(org, "reviewer-1", ROLES),
        now=NOW,
    )

    version = await session.get(OntologyVersion, created.id)
    head = (await session.scalars(select(OntologyHead))).one()
    assert version is not None
    assert (version.status, version.approved_by) == ("APPROVED", "reviewer-1")
    assert head.published_version == version.version == 1


async def test_a_new_version_must_keep_every_published_key(session: AsyncSession) -> None:
    org, _datasource, _schema = await seed_estate(session)
    await session.commit()
    created = await _create(session, org, "customer", "account")
    submitted = await _submit(session, org, created.id)
    review = await session.get(GovernanceReview, submitted.governance_review_id)
    assert review is not None
    await decide_review(
        session,
        review,
        decision="APPROVE",
        reason="ok",
        context=human(org, "reviewer-1", ROLES),
        now=NOW,
    )

    with pytest.raises(HTTPException) as dropped:
        await _create(session, org, "customer", base_version=1)
    with pytest.raises(HTTPException) as stale:
        await _create(session, org, "customer", "account")

    assert dropped.value.status_code == 422
    assert stale.value.status_code == 409


def test_an_ontology_version_is_published_meaning_no_agent_decides() -> None:
    assert risk_tier_for("ONTOLOGY_VERSION") == TIER_T2
    for ceiling in ("T0", "T1", "T2", "T3"):
        assert "ONTOLOGY_VERSION" not in agent_decidable_object_types(ceiling)
