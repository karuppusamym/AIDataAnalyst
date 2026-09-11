"""AR-03: an agent approval rests on evidence about the proposal, not about its label.

Places where the reviewer agent's evidence said something other than what an
approval needed:

* A document claim's `confidence` is the certainty of the structural *name
  match* -- 1.0 for any data-dictionary row whose table and column names matched
  exactly. It says the claim is about the right column. It says nothing about
  whether the row's description is true, and nothing in the platform scores
  that. With `confidence` as its evidence, the agent approved every matched row
  of any uploaded dictionary, wrong descriptions included. It now abstains, and
  a person decides.
* The name match itself was looser than its 1.0 claimed. Columns were matched
  with `ILIKE`, which reads `%` and `_` in a spreadsheet cell as wildcards, so a
  column cell of `%` matched whichever column the database returned first.
* A query-history metric candidate publishes a real `SemanticMetric` when
  approved, exactly as a metric proposal does, but was one tier lower (T1
  against T2), inside the agent's ceiling. It is now T2.

The false-approval measurement across every type the agent can reach is
`tests/test_ar03_false_approval_benchmark.py`.
"""

from collections.abc import AsyncIterator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida import reviewer_agent
from aida.config import Settings
from aida.db import Base
from aida.document_ingestion_api import (
    DocumentCreate,
    extract_claims,
    map_document,
    upload_document,
)
from aida.models import DocumentClaim, DocumentMapping, DocumentSection, GovernanceReview
from aida.review_risk_tiers import TIER_T2, agent_decidable_object_types, risk_tier_for
from tests.test_document_ingestion import (
    _DICTIONARY_CSV,
    _context,
    _seed_column,
    _seed_datasource,
    _seed_project,
    _seed_table,
)


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


def _agent_settings() -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "reviewer_agent_enabled": True,
        "reviewer_agent_principal_id": "agent:reviewer",
        "reviewer_agent_max_tier": "T1",
    }
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


async def test_the_agent_abstains_on_a_document_claim_whatever_its_match_confidence(
    session: AsyncSession,
) -> None:
    project = await _seed_project(session)
    datasource = await _seed_datasource(session, project, name="primary")
    table = await _seed_table(session, datasource, name="customers")
    await _seed_column(session, table, name="customer_id")
    await _seed_column(session, table, name="ssn")
    context = _context(project)
    document = await upload_document(
        project.id,
        DocumentCreate(filename="dictionary.csv", content=_DICTIONARY_CSV),
        context,
        session,
    )
    await map_document(document.id, context, session)
    await extract_claims(document.id, context, session)
    claims = (await session.scalars(select(DocumentClaim))).all()
    # The number the agent used to read: every matched row is a certain match.
    assert claims and all(claim.confidence == 1.0 for claim in claims)

    for claim in claims:
        review = await session.get(GovernanceReview, claim.governance_review_id)
        assert review is not None
        assessment = await reviewer_agent._assess(
            session, review, settings=_agent_settings(), ceiling="T1"
        )
        assert assessment.recommendation == "NONE"
        assert assessment.evidence["evidence_reason"] == reviewer_agent.EVIDENCE_NO_RESOLVER


async def test_a_wildcard_in_a_dictionary_column_cell_matches_no_column(
    session: AsyncSession,
) -> None:
    project = await _seed_project(session)
    datasource = await _seed_datasource(session, project, name="primary")
    table = await _seed_table(session, datasource, name="customers")
    column = await _seed_column(session, table, name="customer_id")
    context = _context(project)
    document = await upload_document(
        project.id,
        DocumentCreate(
            filename="dictionary.csv",
            content=(
                "schema,table,column,description\n"
                "public,customers,%,whatever the first column is\n"
                "public,customers,customer_i_,one character off\n"
                "public,customers,CUSTOMER_ID,the same name in another case\n"
            ),
        ),
        context,
        session,
    )

    await map_document(document.id, context, session)

    rows = (
        await session.execute(
            select(
                DocumentSection.raw_column_name,
                DocumentMapping.mapping_kind,
                DocumentMapping.subject_id,
            )
            .join(DocumentMapping, DocumentMapping.document_section_id == DocumentSection.id)
            .where(DocumentSection.document_id == document.id)
        )
    ).all()
    by_cell = {cell: (kind, subject) for cell, kind, subject in rows}
    assert by_cell["%"] == ("UNMATCHED", None)
    assert by_cell["customer_i_"] == ("UNMATCHED", None)
    # Casefolded equality keeps the one thing ILIKE was there for.
    assert by_cell["CUSTOMER_ID"] == ("STRUCTURAL", str(column.id))


def test_a_mined_metric_candidate_is_the_same_tier_as_a_metric_proposal() -> None:
    assert risk_tier_for("QUERY_HISTORY_METRIC_CANDIDATE") == TIER_T2
    assert risk_tier_for("SEMANTIC_METRIC_PROPOSAL") == TIER_T2


def test_publishing_a_metric_is_outside_the_agents_reach_either_way() -> None:
    decidable = agent_decidable_object_types("T1")

    assert "QUERY_HISTORY_METRIC_CANDIDATE" not in decidable
    assert "SEMANTIC_METRIC_PROPOSAL" not in decidable
    # A document claim is still T1, so it stays in the queue the agent reads;
    # the agent abstains on it for want of evidence, not because of its tier.
    assert "DOCUMENT_CLAIM" in decidable
