"""Retiring an approved description, through the same review every publish uses.

The gap this closes was flagged twice before it was built: publishing was
governed from the first commit, un-publishing was not possible at all. The
tests that matter most here are the ones asserting what withdrawal does *not*
do -- it does not delete the text, and it does not retire content the reviewer
never read.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.asset_description_service import publish_asset_documentation_version
from aida.column_documentation import (
    current_descriptions_by_column_id,
    publish_column_description,
)
from aida.column_documentation_api import get_table_description, list_column_documentation
from aida.config import Settings
from aida.db import Base
from aida.description_withdrawal import (
    WITHDRAWN,
    apply_description_withdrawal,
    request_description_withdrawal,
)
from aida.description_withdrawal_api import (
    DescriptionWithdrawalCreate,
    create_description_withdrawal,
    list_description_withdrawals,
)
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    AuditEvent,
    ColumnDocumentationVersion,
    DataDomain,
    DataSource,
    DescriptionWithdrawal,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.schemas import GovernanceDecisionRequest
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review

_SETTINGS = Settings()
_MAKER = "maker@example.com"
_CHECKER = "checker@example.com"

_audit_event_ids = itertools.count(1)


@event.listens_for(AuditEvent, "before_insert")
def _assign_audit_event_id(mapper: object, connection: object, target: AuditEvent) -> None:
    if target.id is None:
        target.id = next(_audit_event_ids)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as db_session:
        yield db_session
    await engine.dispose()


async def _seed(session: AsyncSession) -> tuple[MetadataTable, MetadataColumn]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"R{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Retail",
        code=f"D{uuid4().hex[:6]}",
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Core",
        slug=f"core-{uuid4().hex[:8]}",
    )
    session.add_all([org, lob, domain, project])
    await session.flush()
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name="wh",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://X",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id, name="c", fingerprint="f"
    )
    session.add_all([datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="f"
    )
    session.add(schema)
    await session.flush()
    table = MetadataTable(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name="customers",
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="f",
    )
    session.add(table)
    await session.flush()
    column = MetadataColumn(
        id=uuid4(),
        organization_id=org.id,
        table_id=table.id,
        name="customer_id",
        ordinal_position=0,
        physical_type="uuid",
        nullable=False,
        source_description="pk",
        status="ACTIVE",
        fingerprint="f",
    )
    session.add(column)
    await session.flush()
    return table, column


def _context(organization_id, principal: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset({"DataSteward"}),
    )


async def _describe(session, table, column, text="An approved description.") -> None:
    await publish_column_description(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        column_id=column.id,
        description=text,
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )


async def _request(session, table, column, reason="It describes the wrong column."):
    return await request_description_withdrawal(
        session,
        organization_id=table.organization_id,
        subject_type="COLUMN",
        subject_id=column.id,
        reason=reason,
        requested_by=_MAKER,
    )


# ---------------------------------------------------------------------------
# Raising a withdrawal
# ---------------------------------------------------------------------------


async def test_requesting_a_withdrawal_publishes_nothing_yet(session) -> None:
    table, column = await _seed(session)
    await _describe(session, table, column)

    withdrawal, review = await _request(session, table, column)

    assert withdrawal.status == "PENDING_REVIEW"
    assert review.object_type == "DESCRIPTION_WITHDRAWAL"
    assert review.requested_by == _MAKER
    # Still published, still what every reader resolves.
    resolved = await current_descriptions_by_column_id(session, [column.id])
    assert resolved[column.id].description == "An approved description."


async def test_the_request_records_the_exact_version_and_text_it_names(session) -> None:
    """Recorded, not looked up later: this is what lets approval refuse to
    retire content the reviewer never read.
    """
    table, column = await _seed(session)
    await _describe(session, table, column)
    current = (await current_descriptions_by_column_id(session, [column.id]))[column.id]

    withdrawal, _ = await _request(session, table, column)

    assert withdrawal.version_id == current.id
    assert withdrawal.withdrawn_text == "An approved description."
    assert withdrawal.subject_label == "customers.customer_id"


async def test_withdrawing_an_undescribed_column_is_refused(session) -> None:
    """A reviewer should never be handed a decision whose subject does not
    exist, so this refuses rather than filing an empty review.
    """
    table, column = await _seed(session)
    with pytest.raises(HTTPException) as exc_info:
        await _request(session, table, column)
    assert exc_info.value.status_code == 409


async def test_a_second_pending_withdrawal_for_the_same_asset_is_refused(session) -> None:
    table, column = await _seed(session)
    await _describe(session, table, column)
    await _request(session, table, column)

    with pytest.raises(HTTPException) as exc_info:
        await _request(session, table, column)
    assert exc_info.value.status_code == 409
    assert "already awaiting review" in exc_info.value.detail


# ---------------------------------------------------------------------------
# Deciding it
# ---------------------------------------------------------------------------


async def test_approval_retires_the_description_without_deleting_it(session) -> None:
    """The central guarantee: a run grounded on this text stays replayable
    against exactly the words it saw.
    """
    table, column = await _seed(session)
    await _describe(session, table, column)
    withdrawal, review = await _request(session, table, column)
    await session.commit()

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    # No longer resolves as the column's description...
    assert await current_descriptions_by_column_id(session, [column.id]) == {}
    # ...but the row and its content are still there.
    version = await session.scalar(
        select(ColumnDocumentationVersion).where(
            ColumnDocumentationVersion.id == withdrawal.version_id
        )
    )
    assert version is not None
    assert version.status == WITHDRAWN
    assert version.description == "An approved description."


async def test_withdrawn_is_distinct_from_superseded(session) -> None:
    """An audit has to be able to tell a retraction from a replacement."""
    table, column = await _seed(session)
    await _describe(session, table, column, "First.")
    await _describe(session, table, column, "Second.")
    _, review = await _request(session, table, column)
    await session.commit()

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    versions = {
        v.description: v.status
        for v in (await session.execute(select(ColumnDocumentationVersion))).scalars().all()
    }
    assert versions == {"First.": "SUPERSEDED", "Second.": WITHDRAWN}


async def test_rejecting_a_withdrawal_leaves_the_description_published(session) -> None:
    table, column = await _seed(session)
    await _describe(session, table, column)
    withdrawal, review = await _request(session, table, column)
    await session.commit()

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="REJECT", reason="the description is correct"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    resolved = await current_descriptions_by_column_id(session, [column.id])
    assert resolved[column.id].description == "An approved description."
    await session.refresh(withdrawal)
    assert withdrawal.status == "REJECTED"


async def test_a_description_republished_before_approval_is_not_retired(session) -> None:
    """The reviewer read one description; without this check they would be
    removing another. Same lost-update reasoning as the workbook import's
    `expected_version`.
    """
    table, column = await _seed(session)
    await _describe(session, table, column, "The text the reviewer read.")
    withdrawal, review = await _request(session, table, column)
    await session.commit()

    # Someone publishes a correction in the window.
    await _describe(session, table, column, "A corrected description.")

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    resolved = await current_descriptions_by_column_id(session, [column.id])
    assert resolved[column.id].description == "A corrected description."
    await session.refresh(withdrawal)
    # The request is closed, but nothing was retired.
    assert withdrawal.status == "APPROVED"
    assert withdrawal.reviewed_by == _CHECKER


async def test_the_requester_cannot_approve_their_own_withdrawal(session) -> None:
    table, column = await _seed(session)
    await _describe(session, table, column)
    _, review = await _request(session, table, column)
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await decide_governance_review(
            review.id,
            GovernanceDecisionRequest(decision="APPROVE"),
            _context(table.organization_id, _MAKER),
            session,
        )
    assert exc_info.value.status_code == 409
    assert (await current_descriptions_by_column_id(session, [column.id])) != {}


async def test_a_table_readme_can_be_withdrawn_too(session) -> None:
    table, _ = await _seed(session)
    await publish_asset_documentation_version(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        readme="Customer master, loaded nightly.",
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )
    _, review = await request_description_withdrawal(
        session,
        organization_id=table.organization_id,
        subject_type="TABLE",
        subject_id=table.id,
        reason="superseded by the data contract",
        requested_by=_MAKER,
    )
    await session.commit()

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    version = await session.scalar(
        select(AssetDocumentationVersion).join(
            AssetDocumentation,
            AssetDocumentation.id == AssetDocumentationVersion.documentation_id,
        )
    )
    assert version is not None
    assert version.status == WITHDRAWN
    assert version.readme == "Customer master, loaded nightly."


async def test_applying_a_decided_withdrawal_twice_is_refused(session) -> None:
    table, column = await _seed(session)
    await _describe(session, table, column)
    withdrawal, review = await _request(session, table, column)
    await session.commit()
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    with pytest.raises(HTTPException) as exc_info:
        await apply_description_withdrawal(
            session, withdrawal, reviewer=_CHECKER, now=datetime.now(UTC)
        )
    assert exc_info.value.status_code == 409


# ---------------------------------------------------------------------------
# What a reader sees afterwards
# ---------------------------------------------------------------------------


async def test_a_withdrawn_column_reads_as_retired_not_as_never_documented(session) -> None:
    """Materially different facts: "we looked and decided to say nothing"
    versus "nobody has looked".
    """
    table, column = await _seed(session)
    await _describe(session, table, column, "The retired text.")
    _, review = await _request(session, table, column)
    await session.commit()
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    page = await list_column_documentation(
        table.id,
        limit=200,
        offset=0,
        context=_context(table.organization_id, _CHECKER),
        session=session,
        settings=_SETTINGS,
    )
    item = page.items[0]
    assert item.business_description is None
    assert item.withdrawn_description == "The retired text."
    # The source comment is untouched by any of this.
    assert item.source_description == "pk"


async def test_a_column_that_was_never_described_reports_no_withdrawal(session) -> None:
    table, _ = await _seed(session)
    page = await list_column_documentation(
        table.id,
        limit=200,
        offset=0,
        context=_context(table.organization_id, _CHECKER),
        session=session,
        settings=_SETTINGS,
    )
    assert page.items[0].business_description is None
    assert page.items[0].withdrawn_description is None


# ---------------------------------------------------------------------------
# The endpoint
# ---------------------------------------------------------------------------


async def test_endpoint_files_a_request_and_lists_it(session) -> None:
    table, column = await _seed(session)
    await _describe(session, table, column)
    context = _context(table.organization_id, _MAKER)

    created = await create_description_withdrawal(
        DescriptionWithdrawalCreate(
            subject_type="COLUMN", subject_id=column.id, reason="wrong column"
        ),
        context=context,
        session=session,
        settings=_SETTINGS,
    )
    assert created.status == "PENDING_REVIEW"

    page = await list_description_withdrawals(
        subject_id=column.id,
        withdrawal_status=None,
        limit=100,
        offset=0,
        context=context,
        session=session,
    )
    assert page.total == 1
    assert page.items[0].subject_label == "customers.customer_id"
    assert page.items[0].withdrawn_text == "An approved description."


async def test_endpoint_refuses_a_subject_in_another_organization(session) -> None:
    table, column = await _seed(session)
    await _describe(session, table, column)
    foreign = SecurityContext(
        principal_id=_MAKER,
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"DataSteward"}),
    )
    with pytest.raises(HTTPException) as exc_info:
        await create_description_withdrawal(
            DescriptionWithdrawalCreate(
                subject_type="COLUMN", subject_id=column.id, reason="not mine"
            ),
            context=foreign,
            session=session,
            settings=_SETTINGS,
        )
    assert exc_info.value.status_code in (403, 404)
    assert (await session.execute(select(DescriptionWithdrawal))).scalars().all() == []


# ---------------------------------------------------------------------------
# Reinstatement
# ---------------------------------------------------------------------------


async def _withdraw_and_approve(session, table, column) -> None:
    _, review = await _request(session, table, column)
    await session.commit()
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )


async def _reinstate(session, table, column, reason="withdrawn by mistake"):
    return await request_description_withdrawal(
        session,
        organization_id=table.organization_id,
        subject_type="COLUMN",
        subject_id=column.id,
        reason=reason,
        requested_by=_MAKER,
        request_type="REINSTATE",
    )


async def test_reinstating_republishes_as_a_new_version_not_a_status_flip(session) -> None:
    """The version chain has to go on recording that the description was
    retired -- flipping the old row back would rewrite history, and an audit of
    why an agent cited text that "was always approved" would be misled.
    """
    table, column = await _seed(session)
    await _describe(session, table, column, "The original text.")
    await _withdraw_and_approve(session, table, column)

    _, review = await _reinstate(session, table, column)
    await session.commit()
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    versions = (
        (
            await session.execute(
                select(ColumnDocumentationVersion).order_by(ColumnDocumentationVersion.version)
            )
        )
        .scalars()
        .all()
    )
    assert [(v.version, v.status) for v in versions] == [(1, WITHDRAWN), (2, "APPROVED")]
    assert versions[1].description == "The original text."
    # Provenance: the requester authored the reinstatement, the reviewer approved.
    assert versions[1].created_by == _MAKER
    assert versions[1].approved_by == _CHECKER
    resolved = await current_descriptions_by_column_id(session, [column.id])
    assert resolved[column.id].description == "The original text."


async def test_reinstating_a_column_that_has_a_live_description_is_refused(session) -> None:
    """Republishing over a live description is a correction, which is authored
    rather than undone.
    """
    table, column = await _seed(session)
    await _describe(session, table, column)

    with pytest.raises(HTTPException) as exc_info:
        await _reinstate(session, table, column)
    assert exc_info.value.status_code == 409
    assert "publish a correction" in exc_info.value.detail


async def test_reinstating_a_column_that_was_never_withdrawn_is_refused(session) -> None:
    table, column = await _seed(session)
    with pytest.raises(HTTPException) as exc_info:
        await _reinstate(session, table, column)
    assert exc_info.value.status_code == 409
    assert "no withdrawn description" in exc_info.value.detail


async def test_a_reinstatement_is_skipped_if_the_column_was_described_again(session) -> None:
    """Someone wrote a fresh description while the reinstatement was pending;
    bringing the old text back would silently replace theirs.
    """
    table, column = await _seed(session)
    await _describe(session, table, column, "The original text.")
    await _withdraw_and_approve(session, table, column)
    _, review = await _reinstate(session, table, column)
    await session.commit()

    await _describe(session, table, column, "Someone wrote this in the meantime.")

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    resolved = await current_descriptions_by_column_id(session, [column.id])
    assert resolved[column.id].description == "Someone wrote this in the meantime."


async def test_rejecting_a_reinstatement_leaves_the_column_undescribed(session) -> None:
    table, column = await _seed(session)
    await _describe(session, table, column, "The original text.")
    await _withdraw_and_approve(session, table, column)
    withdrawal, review = await _reinstate(session, table, column)
    await session.commit()

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="REJECT", reason="it was withdrawn for a reason"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    assert await current_descriptions_by_column_id(session, [column.id]) == {}
    await session.refresh(withdrawal)
    assert (withdrawal.status, withdrawal.request_type) == ("REJECTED", "REINSTATE")


async def test_the_requester_cannot_approve_their_own_reinstatement(session) -> None:
    table, column = await _seed(session)
    await _describe(session, table, column, "The original text.")
    await _withdraw_and_approve(session, table, column)
    _, review = await _reinstate(session, table, column)
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await decide_governance_review(
            review.id,
            GovernanceDecisionRequest(decision="APPROVE"),
            _context(table.organization_id, _MAKER),
            session,
        )
    assert exc_info.value.status_code == 409
    assert await current_descriptions_by_column_id(session, [column.id]) == {}


async def test_a_table_readme_can_be_reinstated_too(session) -> None:
    table, _ = await _seed(session)
    await publish_asset_documentation_version(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        readme="Customer master, loaded nightly.",
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )
    _, withdraw_review = await request_description_withdrawal(
        session,
        organization_id=table.organization_id,
        subject_type="TABLE",
        subject_id=table.id,
        reason="superseded",
        requested_by=_MAKER,
    )
    await session.commit()
    await decide_governance_review(
        withdraw_review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    _, reinstate_review = await request_description_withdrawal(
        session,
        organization_id=table.organization_id,
        subject_type="TABLE",
        subject_id=table.id,
        reason="withdrawn by mistake",
        requested_by=_MAKER,
        request_type="REINSTATE",
    )
    await session.commit()
    await decide_governance_review(
        reinstate_review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    versions = (
        (
            await session.execute(
                select(AssetDocumentationVersion).order_by(AssetDocumentationVersion.version)
            )
        )
        .scalars()
        .all()
    )
    assert [(v.version, v.status) for v in versions] == [(1, WITHDRAWN), (2, "APPROVED")]
    assert versions[1].readme == "Customer master, loaded nightly."


async def test_an_unknown_request_type_is_refused(session) -> None:
    table, column = await _seed(session)
    await _describe(session, table, column)
    with pytest.raises(HTTPException) as exc_info:
        await request_description_withdrawal(
            session,
            organization_id=table.organization_id,
            subject_type="COLUMN",
            subject_id=column.id,
            reason="x",
            requested_by=_MAKER,
            request_type="DELETE",
        )
    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# The table-level description read
# ---------------------------------------------------------------------------


async def test_table_description_endpoint_reports_the_current_readme(session) -> None:
    table, _ = await _seed(session)
    await publish_asset_documentation_version(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        readme="Customer master.",
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )

    read = await get_table_description(
        table.id,
        context=_context(table.organization_id, _CHECKER),
        session=session,
        settings=_SETTINGS,
    )
    assert read.readme == "Customer master."
    assert read.readme_version == 1
    assert read.approved_by == _CHECKER
    assert read.withdrawn_readme is None


async def test_table_description_endpoint_distinguishes_retired_from_never_written(
    session,
) -> None:
    table, _ = await _seed(session)
    await publish_asset_documentation_version(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        readme="The retired readme.",
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )
    _, review = await request_description_withdrawal(
        session,
        organization_id=table.organization_id,
        subject_type="TABLE",
        subject_id=table.id,
        reason="superseded",
        requested_by=_MAKER,
    )
    await session.commit()
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )

    read = await get_table_description(
        table.id,
        context=_context(table.organization_id, _CHECKER),
        session=session,
        settings=_SETTINGS,
    )
    assert read.readme is None
    assert read.withdrawn_readme == "The retired readme."


async def test_table_description_endpoint_on_an_undocumented_table(session) -> None:
    table, _ = await _seed(session)
    read = await get_table_description(
        table.id,
        context=_context(table.organization_id, _CHECKER),
        session=session,
        settings=_SETTINGS,
    )
    assert (read.readme, read.withdrawn_readme) == (None, None)
    assert read.name == "customers"
