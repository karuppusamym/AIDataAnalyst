"""AT-14: sampling-based bulk review for drafted prose (module 08, GL-9's
`AssetDescriptionDraft` pipeline -- language fields only).

Two endpoints under test, both real async FastAPI handler functions called
directly against a real (in-memory sqlite) database -- the same pattern
`test_bulk_governance_decisions.py` (PG-3) and `test_catalog_rows_read_model.py`
use, not a hand-simulated session:

  * `draw_asset_description_sample_review` -- draws a reproducible sample
    from a batch of PENDING_APPROVAL drafts and audits the draw, deciding
    nothing.
  * `decide_asset_description_sample_review` -- recomputes that same sample
    from (batch, sample_size, seed) and applies ONE decision to exactly the
    sampled items, via the identical `_apply_governance_review_decision`
    core PG-3 and the single-item endpoint use, leaving unsampled items
    untouched and PENDING_APPROVAL.

What this file proves:

1. The endpoint's drawn sample matches `aida.sampling_review.draw_reproducible_sample`
   called directly with the same (batch, sample_size, seed) -- the API layer
   does not silently do something else.
2. The seed and the drawn member ids land in the `AuditEvent` record itself
   (`record_audit`), not only on the response or the review row -- the
   row's own "in the audit record" requirement, checked directly against a
   queried `AuditEvent.details` JSON payload.
3. Only the sampled drafts are finalized (APPROVED/REJECTED); every
   unsampled draft in the batch stays PENDING_APPROVAL with its
   GovernanceReview still PENDING -- the 0.70/no-auto-publish cap this row
   must not relax: model output for an item nobody reviewed never becomes
   authoritative just because a sample of its siblings passed.
4. Self-approval within the sample fails that one item (maker-checker),
   partial-success style, without aborting the rest of the sample.
"""

import itertools
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.asset_description_api import (
    AssetDescriptionSampleDecide,
    AssetDescriptionSampleDraw,
    decide_asset_description_sample_review,
    draw_asset_description_sample_review,
)
from aida.db import Base
from aida.models import (
    AssetDescriptionDraft,
    AuditEvent,
    DataDomain,
    DataSource,
    GovernanceReview,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.sampling_review import draw_reproducible_sample
from aida.security_types import SecurityContext

pytestmark = pytest.mark.asyncio

# `AuditEvent.id` is a `BigInteger` autoincrement primary key that relies in
# production on Postgres's own identity/sequence generation; in-memory
# sqlite leaves it NULL on insert. Same workaround as
# `test_bulk_governance_decisions.py` -- assign ids by hand for this test
# module's sqlite engine only, nothing about the production model changes.
_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _context(org_id: object, principal: str = "reviewer") -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=org_id,
        roles=frozenset({"DataSteward"}),
    )


async def _seed_org_and_datasource(session: AsyncSession) -> tuple[Organization, DataSource]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Ungoverned",
        code=f"UNG{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Warehouse",
        slug=f"wh-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=f"src-{uuid4().hex[:8]}",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    datasource._test_schema = schema  # type: ignore[attr-defined]
    return org, datasource


async def _seed_pending_batch(
    session: AsyncSession,
    org: Organization,
    datasource: DataSource,
    *,
    n: int,
    requested_by: str = "drafter",
) -> list[AssetDescriptionDraft]:
    """Seed `n` PENDING_APPROVAL AssetDescriptionDraft rows, each on its own
    table, each with a PENDING GovernanceReview -- exactly the state
    `asset_description_api.submit_asset_description_draft` leaves a draft in.
    """
    schema = datasource._test_schema  # type: ignore[attr-defined]
    drafts: list[AssetDescriptionDraft] = []
    for index in range(n):
        table = MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=f"t_{index}",
            object_type="BASE_TABLE",
            fingerprint="fp",
        )
        session.add(table)
        await session.flush()
        draft = AssetDescriptionDraft(
            id=uuid4(),
            organization_id=org.id,
            table_id=table.id,
            drafted_text=f"Draft text for table {index}.",
            text_fingerprint=f"fp-{index}",
            accuracy_score=0.8,
            clarity_score=0.8,
            style_score=0.8,
            completeness_score=0.8,
            overall_score=0.8,
            evidence={},
            status="PENDING_APPROVAL",
            created_by=requested_by,
        )
        session.add(draft)
        await session.flush()
        review = GovernanceReview(
            organization_id=org.id,
            object_type="ASSET_DESCRIPTION_DRAFT",
            object_id=str(draft.id),
            requested_action="PUBLISH",
            requested_by=requested_by,
            status="PENDING",
        )
        session.add(review)
        await session.flush()
        draft.governance_review_id = review.id
        drafts.append(draft)
    await session.commit()
    return drafts


async def _audit_events(session: AsyncSession, action: str) -> list[AuditEvent]:
    rows = (
        await session.scalars(select(AuditEvent).where(AuditEvent.action == action))
    ).all()
    return list(rows)


# ---------------------------------------------------------------------------
# 1 & 2: the draw matches the pure function, and lands in the audit record.
# ---------------------------------------------------------------------------


async def test_drawn_sample_matches_the_pure_function_and_is_audited(session: AsyncSession) -> None:
    org, datasource = await _seed_org_and_datasource(session)
    drafts = await _seed_pending_batch(session, org, datasource, n=40)
    draft_ids = [draft.id for draft in drafts]
    seed = 20260901

    expected_drawn = draw_reproducible_sample(draft_ids, sample_size=8, seed=seed)

    result = await draw_asset_description_sample_review(
        org.id,
        AssetDescriptionSampleDraw(draft_ids=draft_ids, sample_size=8, seed=seed),
        context=_context(org.id, "steward-1"),
        session=session,
    )

    assert result.seed == seed
    assert result.batch_size == 40
    assert result.sample_size == 8
    assert result.drawn_draft_ids == expected_drawn
    assert {row.id for row in result.drawn_drafts} == set(expected_drawn)

    events = await _audit_events(session, "asset_description.sample_review.draw")
    assert len(events) == 1
    details = events[0].details
    assert details["seed"] == seed
    assert details["batch_size"] == 40
    assert details["sample_size"] == 8
    assert sorted(details["drawn_draft_ids"]) == sorted(str(value) for value in expected_drawn)
    assert details["seed_was_caller_supplied"] is True


async def test_draw_without_a_seed_generates_and_returns_one(session: AsyncSession) -> None:
    org, datasource = await _seed_org_and_datasource(session)
    drafts = await _seed_pending_batch(session, org, datasource, n=10)
    draft_ids = [draft.id for draft in drafts]

    result = await draw_asset_description_sample_review(
        org.id,
        AssetDescriptionSampleDraw(draft_ids=draft_ids, sample_size=3, seed=None),
        context=_context(org.id, "steward-1"),
        session=session,
    )
    assert isinstance(result.seed, int)
    # Recomputing with the returned seed must reproduce the exact same draw.
    assert draw_reproducible_sample(draft_ids, sample_size=3, seed=result.seed) == (
        result.drawn_draft_ids
    )


async def test_draw_rejects_a_batch_with_a_non_pending_draft(session: AsyncSession) -> None:
    org, datasource = await _seed_org_and_datasource(session)
    drafts = await _seed_pending_batch(session, org, datasource, n=3)
    drafts[0].status = "APPROVED"
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await draw_asset_description_sample_review(
            org.id,
            AssetDescriptionSampleDraw(
                draft_ids=[draft.id for draft in drafts], sample_size=2, seed=1
            ),
            context=_context(org.id, "steward-1"),
            session=session,
        )
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# 3: only the sampled subset is finalized; the rest stays pending.
# ---------------------------------------------------------------------------


async def test_decide_finalizes_only_the_sampled_subset_and_leaves_the_rest_pending(
    session: AsyncSession,
) -> None:
    org, datasource = await _seed_org_and_datasource(session)
    drafts = await _seed_pending_batch(session, org, datasource, n=50, requested_by="drafter")
    draft_ids = [draft.id for draft in drafts]
    seed = 777

    expected_drawn = draw_reproducible_sample(draft_ids, sample_size=5, seed=seed)
    expected_unsampled = [d for d in draft_ids if d not in set(expected_drawn)]

    result = await decide_asset_description_sample_review(
        org.id,
        AssetDescriptionSampleDecide(
            draft_ids=draft_ids,
            sample_size=5,
            seed=seed,
            decision="APPROVE",
        ),
        context=_context(org.id, "steward-1"),
        session=session,
    )

    assert result.seed == seed
    assert result.batch_size == 50
    assert result.sample_size == 5
    assert result.drawn_draft_ids == expected_drawn
    assert result.unsampled_draft_ids == expected_unsampled
    assert result.succeeded_count == 5
    assert result.failed_count == 0

    # Re-fetch every draft from the database -- the real, committed state.
    all_drafts = {
        row.id: row
        for row in (
            await session.scalars(
                select(AssetDescriptionDraft).where(AssetDescriptionDraft.id.in_(draft_ids))
            )
        ).all()
    }
    for drawn_id in expected_drawn:
        assert all_drafts[drawn_id].status == "APPROVED"
        assert all_drafts[drawn_id].published_version_id is not None
        assert all_drafts[drawn_id].evidence["sample_review"]["seed"] == seed
    for unsampled_id in expected_unsampled:
        assert all_drafts[unsampled_id].status == "PENDING_APPROVAL"
        assert all_drafts[unsampled_id].published_version_id is None

    # And every unsampled item's GovernanceReview is still PENDING.
    unsampled_review_ids = [all_drafts[d].governance_review_id for d in expected_unsampled]
    reviews = (
        await session.scalars(
            select(GovernanceReview).where(GovernanceReview.id.in_(unsampled_review_ids))
        )
    ).all()
    assert all(review.status == "PENDING" for review in reviews)


async def test_decide_audit_record_carries_the_seed_and_drawn_ids(session: AsyncSession) -> None:
    """The row's own requirement, checked directly: the audit record -- not
    just the response, not just the per-draft evidence field -- carries the
    seed and the exact drawn member ids."""
    org, datasource = await _seed_org_and_datasource(session)
    drafts = await _seed_pending_batch(session, org, datasource, n=30)
    draft_ids = [draft.id for draft in drafts]
    seed = 555111

    expected_drawn = draw_reproducible_sample(draft_ids, sample_size=6, seed=seed)

    await decide_asset_description_sample_review(
        org.id,
        AssetDescriptionSampleDecide(
            draft_ids=draft_ids, sample_size=6, seed=seed, decision="REJECT", reason="spot check"
        ),
        context=_context(org.id, "steward-1"),
        session=session,
    )

    events = await _audit_events(session, "asset_description.sample_review.decide")
    assert len(events) == 1
    details = events[0].details
    assert details["seed"] == seed
    assert details["decision"] == "REJECT"
    assert details["sample_size"] == 6
    assert details["batch_size"] == 30
    assert sorted(details["drawn_draft_ids"]) == sorted(str(value) for value in expected_drawn)
    assert details["succeeded_count"] == 6
    assert details["failed_count"] == 0

    # And the rejection actually applied only to the drawn ids.
    rejected = (
        await session.scalars(
            select(AssetDescriptionDraft).where(AssetDescriptionDraft.status == "REJECTED")
        )
    ).all()
    assert {row.id for row in rejected} == set(expected_drawn)


async def test_decide_same_seed_is_reproducible_across_separate_draw_and_decide_calls(
    session: AsyncSession,
) -> None:
    org, datasource = await _seed_org_and_datasource(session)
    drafts = await _seed_pending_batch(session, org, datasource, n=25)
    draft_ids = [draft.id for draft in drafts]
    seed = 3141592

    draw_result = await draw_asset_description_sample_review(
        org.id,
        AssetDescriptionSampleDraw(draft_ids=draft_ids, sample_fraction=0.2, seed=seed),
        context=_context(org.id, "steward-1"),
        session=session,
    )
    decide_result = await decide_asset_description_sample_review(
        org.id,
        AssetDescriptionSampleDecide(
            draft_ids=draft_ids, sample_fraction=0.2, seed=seed, decision="APPROVE"
        ),
        context=_context(org.id, "steward-1"),
        session=session,
    )
    assert draw_result.drawn_draft_ids == decide_result.drawn_draft_ids


# ---------------------------------------------------------------------------
# 4: self-approval within the sample fails that one item, not the batch.
# ---------------------------------------------------------------------------


async def test_self_approval_within_the_sample_fails_only_that_item(session: AsyncSession) -> None:
    org, datasource = await _seed_org_and_datasource(session)
    drafts = await _seed_pending_batch(session, org, datasource, n=20, requested_by="steward-1")
    draft_ids = [draft.id for draft in drafts]
    seed = 42

    result = await decide_asset_description_sample_review(
        org.id,
        AssetDescriptionSampleDecide(
            draft_ids=draft_ids, sample_size=5, seed=seed, decision="APPROVE"
        ),
        # The same principal that "requested" every review (drafted every item):
        # self-approval must be denied per item, not silently allowed.
        context=_context(org.id, "steward-1"),
        session=session,
    )
    assert result.succeeded_count == 0
    assert result.failed_count == 5
    assert all(item.status == "FAILED" for item in result.results)
    assert all("maker-checker" in (item.reason or "") for item in result.results)

    # Nothing was actually decided.
    all_drafts = (
        await session.scalars(
            select(AssetDescriptionDraft).where(AssetDescriptionDraft.id.in_(draft_ids))
        )
    ).all()
    assert all(row.status == "PENDING_APPROVAL" for row in all_drafts)


async def test_decide_rejects_batch_with_sample_size_and_fraction_both_set() -> None:
    with pytest.raises(ValueError):
        AssetDescriptionSampleDecide(
            draft_ids=[uuid4()],
            sample_size=1,
            sample_fraction=0.5,
            seed=1,
            decision="APPROVE",
        )


async def test_decide_reject_without_reason_is_rejected() -> None:
    with pytest.raises(ValueError):
        AssetDescriptionSampleDecide(
            draft_ids=[uuid4()],
            sample_size=1,
            seed=1,
            decision="REJECT",
        )
