"""Retiring an approved description, through the same review every publish uses.

The gap this closes was flagged twice before it was built: publishing was
governed from the first commit, un-publishing was not possible at all. The
tests that matter most here are the ones asserting what withdrawal does *not*
do -- it does not delete the text, and it does not retire content the reviewer
never read.
"""

from __future__ import annotations

import inspect
import itertools
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.asset_description_service import publish_asset_documentation_version
from aida.catalog_read_model import compose_catalog_rows
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
    _authorize_subject,
    create_description_withdrawal,
    list_description_withdrawals,
)
from aida.envelope_models import MetadataRoutine, MetadataRoutineDefinitionVersion
from aida.models import (
    AssetDocumentation,
    AssetDocumentationVersion,
    AuditEvent,
    ColumnDocumentationVersion,
    DataDomain,
    DataSource,
    DescriptionWithdrawal,
    LineOfBusiness,
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.routine_description_service import (
    current_routine_description,
    publish_routine_documentation_version,
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


# ---------------------------------------------------------------------------
# What the catalog row says once a description is retired
# ---------------------------------------------------------------------------
#
# UX-12's `CatalogRowRead.description` is a single collapsed field fed by a
# four-rung precedence chain (`atlas.modules.catalog.service._description`),
# so the withdrawal invariant -- "the asset reads as undescribed again", the
# words `models.DescriptionWithdrawal` uses -- is only true here if retiring
# the top rung does not silently promote a lower one.


async def _document(session, table, readme="Customer master, loaded nightly.") -> None:
    await publish_asset_documentation_version(
        session,
        organization_id=table.organization_id,
        table_id=table.id,
        readme=readme,
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
    )


async def _annotate(session, table, description: str) -> None:
    """Give the table an approved AT-6 business annotation -- the rung the
    catalog row falls through to when there is no GL-9 documentation."""
    annotation = MetadataBusinessAnnotation(
        id=uuid4(),
        organization_id=table.organization_id,
        datasource_id=table.datasource_id,
        table_id=table.id,
        domain_id=uuid4(),
        entity_id=uuid4(),
        source_proposal_id=uuid4(),
    )
    session.add(annotation)
    await session.flush()
    session.add(
        MetadataBusinessAnnotationVersion(
            id=uuid4(),
            organization_id=table.organization_id,
            annotation_id=annotation.id,
            version=1,
            status="APPROVED",
            business_name="Customers",
            business_description=description,
            table_role="DIMENSION",
            grain_statement="One row per customer.",
            confidence=0.9,
            approved_by=_CHECKER,
            approved_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _withdraw_table_description(session, table) -> None:
    _, review = await request_description_withdrawal(
        session,
        organization_id=table.organization_id,
        subject_type="TABLE",
        subject_id=table.id,
        reason="It describes the wrong table.",
        requested_by=_MAKER,
    )
    await session.commit()
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(table.organization_id, _CHECKER),
        session,
    )


async def _catalog_row(session, table):
    rows = await compose_catalog_rows(session, [(table, "public", "wh")])
    return rows[0]


async def test_a_withdrawn_table_is_not_re_described_by_its_business_annotation(
    session,
) -> None:
    table, _ = await _seed(session)
    await _annotate(session, table, "Approved business-annotation description.")
    await _document(session, table)

    assert (await _catalog_row(session, table)).description == "Customer master, loaded nightly."

    await _withdraw_table_description(session, table)

    row = await _catalog_row(session, table)
    assert row.description is None
    assert row.description_is_proposed is False


async def test_a_withdrawal_leaves_the_source_systems_own_comment_showing(session) -> None:
    """The source comment is not this platform speaking.

    Withdrawal retires what we authored and returns the row to what it said
    before anyone here described the table; it does not suppress the source
    system's own comment, which rediscovery re-derives anyway and which
    `get_table_description` goes on reporting beside a retired readme.
    """
    table, _ = await _seed(session)
    table.source_description = "Connector-scanned comment."
    await session.flush()
    await _document(session, table)

    await _withdraw_table_description(session, table)

    row = await _catalog_row(session, table)
    assert row.description == "Connector-scanned comment."
    assert row.description_is_proposed is False


async def test_reinstating_puts_the_description_back_on_the_row(session) -> None:
    """The suppression follows the current version, not the withdrawal record.

    A reinstatement publishes a *new* APPROVED version beside the retired one,
    so the top rung is occupied again and the row must read as described --
    otherwise the WITHDRAWN row left behind by design would mute the asset
    permanently.
    """
    table, _ = await _seed(session)
    await _annotate(session, table, "Approved business-annotation description.")
    await _document(session, table)
    await _withdraw_table_description(session, table)

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

    row = await _catalog_row(session, table)
    assert row.description == "Customer master, loaded nightly."
    assert row.description_is_proposed is False


# ---------------------------------------------------------------------------
# R11-FP08: a routine's description is withdrawn and reinstated the same way.
#
# Templated on `tests/test_r11c8_annotation_withdrawal.py`, which is this
# repository's worked precedent for adding a subject type to this table. The
# interesting part is where routines *diverge* from that precedent: an
# annotation is withdrawn but never reinstated, because nothing on this path
# can publish one back and the better annotation genuinely is a new proposal.
# A routine has a real append-only store with its own publish function, so it
# reinstates -- and, because a published routine description names the
# definition version it was written against, a reinstatement can refuse to
# republish prose about a body that moved while it sat retired. Neither the
# table nor the column store can make that check.
# ---------------------------------------------------------------------------


async def _seed_routine(session, table, *, source_description: str | None = None):
    """One ACTIVE procedure in the same estate `_seed` built, with one captured
    definition version -- the row a published description points at."""
    routine = MetadataRoutine(
        id=uuid4(),
        organization_id=table.organization_id,
        datasource_id=table.datasource_id,
        schema_id=table.schema_id,
        name="sp_load_customers",
        signature="(p_as_of date)",
        routine_type="PROCEDURE",
        body_sql_redacted="CREATE PROCEDURE sp_load_customers() AS BEGIN SELECT ? END",
        body_fingerprint="bf",
        redaction_status="LEXICAL",
        screening_status="CLEAN",
        availability="AVAILABLE",
        source_description=source_description,
        status="ACTIVE",
        fingerprint="f",
    )
    session.add(routine)
    await session.flush()
    session.add(
        MetadataRoutineDefinitionVersion(
            id=uuid4(),
            organization_id=routine.organization_id,
            datasource_id=routine.datasource_id,
            routine_id=routine.id,
            body_sql_redacted=routine.body_sql_redacted,
            body_fingerprint=routine.body_fingerprint,
            availability="AVAILABLE",
            truncated=False,
            redaction_status="LEXICAL",
            screening_status="CLEAN",
            version_number=1,
            captured_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return routine


async def _describe_routine(session, routine, text="Loads the customer master nightly."):
    return await publish_routine_documentation_version(
        session,
        organization_id=routine.organization_id,
        datasource_id=routine.datasource_id,
        routine_id=routine.id,
        description=text,
        created_by=_MAKER,
        approved_by=_CHECKER,
        approved_at=datetime.now(UTC),
        source_definition_version_id=await session.scalar(
            select(MetadataRoutineDefinitionVersion.id)
            .where(MetadataRoutineDefinitionVersion.routine_id == routine.id)
            .order_by(MetadataRoutineDefinitionVersion.version_number.desc())
            .limit(1)
        ),
    )


async def _request_routine(session, routine, *, request_type="WITHDRAW"):
    return await request_description_withdrawal(
        session,
        organization_id=routine.organization_id,
        subject_type="ROUTINE",
        subject_id=routine.id,
        reason="It describes the wrong procedure.",
        requested_by=_MAKER,
        request_type=request_type,
    )


async def test_a_routine_withdrawal_is_raised_against_the_exact_version(session) -> None:
    table, _ = await _seed(session)
    routine = await _seed_routine(session, table)
    version = await _describe_routine(session, routine)

    withdrawal, review = await _request_routine(session, routine)

    assert (withdrawal.subject_type, withdrawal.version_id, withdrawal.status) == (
        "ROUTINE",
        version.id,
        "PENDING_REVIEW",
    )
    assert withdrawal.withdrawn_text == "Loads the customer master nightly."
    # The routine is named with its kind: `sp_load` in two schemas is common,
    # and a reviewer deciding a retraction has to know which object it is about.
    assert withdrawal.subject_label == "sp_load_customers (procedure)"
    assert (review.object_type, review.requested_action) == (
        "DESCRIPTION_WITHDRAWAL",
        "WITHDRAW_DESCRIPTION",
    )
    # Nothing has happened to the description yet.
    assert version.status == "APPROVED"


async def test_approving_a_routine_withdrawal_keeps_the_text_and_stops_resolving_it(
    session,
) -> None:
    """Withdrawal is not a delete: a run grounded on the text stays replayable
    against exactly the words it saw. What changes is that the current-version
    resolver stops returning it."""
    table, _ = await _seed(session)
    routine = await _seed_routine(session, table)
    version = await _describe_routine(session, routine)
    _withdrawal, review = await _request_routine(session, routine)
    await session.commit()

    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(routine.organization_id, _CHECKER),
        session,
    )

    await session.refresh(version)
    assert version.status == WITHDRAWN
    assert version.description == "Loads the customer master nightly."
    assert await current_routine_description(session, routine.id) is None


async def test_a_routine_withdrawal_names_no_routine_body(session) -> None:
    """The snapshot a reviewer reads is the description, never the procedure
    body -- the largest indirect-injection surface in the estate."""
    table, _ = await _seed(session)
    routine = await _seed_routine(session, table)
    await _describe_routine(session, routine)

    withdrawal, _review = await _request_routine(session, routine)

    assert "CREATE PROCEDURE" not in withdrawal.withdrawn_text
    assert "SELECT" not in withdrawal.withdrawn_text


async def test_a_routine_with_no_approved_description_cannot_be_withdrawn(session) -> None:
    table, _ = await _seed(session)
    routine = await _seed_routine(session, table)

    with pytest.raises(HTTPException) as refused:
        await _request_routine(session, routine)

    assert refused.value.status_code == 409
    assert "no approved description" in str(refused.value.detail)


async def test_an_unknown_routine_is_a_404_not_a_missing_description(session) -> None:
    table, _ = await _seed(session)
    await _seed_routine(session, table)

    with pytest.raises(HTTPException) as refused:
        await request_description_withdrawal(
            session,
            organization_id=table.organization_id,
            subject_type="ROUTINE",
            subject_id=uuid4(),
            reason="It describes the wrong procedure.",
            requested_by=_MAKER,
        )

    assert refused.value.status_code == 404
    assert "routine not found" in str(refused.value.detail)


async def test_reinstating_a_routine_description_republishes_it_as_a_new_version(
    session,
) -> None:
    """Never flips the WITHDRAWN row back: the chain has to go on recording that
    the description was retired, or an audit of why a run cited text that "was
    always approved" would be misled."""
    table, _ = await _seed(session)
    routine = await _seed_routine(session, table)
    retired = await _describe_routine(session, routine)
    _withdrawal, review = await _request_routine(session, routine)
    await session.commit()
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(routine.organization_id, _CHECKER),
        session,
    )

    _reinstatement, reinstate_review = await _request_routine(
        session, routine, request_type="REINSTATE"
    )
    await session.commit()
    await decide_governance_review(
        reinstate_review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(routine.organization_id, _CHECKER),
        session,
    )

    await session.refresh(retired)
    assert retired.status == WITHDRAWN
    current = await current_routine_description(session, routine.id)
    assert current is not None
    assert (current.version, current.description) == (2, "Loads the customer master nightly.")
    assert current.id != retired.id


async def test_a_routine_reinstatement_is_refused_once_its_body_has_moved(session) -> None:
    """The check only a routine can make. A description of a procedure is a
    statement about a body, and the body can be redefined while the description
    sits retired; republishing then would put prose about a routine that no
    longer exists back in front of every reader. Reported as approved-but-not-
    applied, the same outcome a table reinstatement reports when someone
    described the asset again in the window."""
    table, _ = await _seed(session)
    routine = await _seed_routine(session, table)
    await _describe_routine(session, routine)
    _withdrawal, review = await _request_routine(session, routine)
    await session.commit()
    await decide_governance_review(
        review.id,
        GovernanceDecisionRequest(decision="APPROVE"),
        _context(routine.organization_id, _CHECKER),
        session,
    )
    reinstatement, reinstate_review = await _request_routine(
        session, routine, request_type="REINSTATE"
    )
    await session.commit()
    # A rescan captured a redefined body: a new immutable definition version.
    session.add(
        MetadataRoutineDefinitionVersion(
            id=uuid4(),
            organization_id=routine.organization_id,
            datasource_id=routine.datasource_id,
            routine_id=routine.id,
            body_sql_redacted="CREATE PROCEDURE sp_load_customers() AS BEGIN UPDATE ? END",
            body_fingerprint="bf2",
            availability="AVAILABLE",
            truncated=False,
            redaction_status="LEXICAL",
            screening_status="CLEAN",
            version_number=2,
            change_class="STRUCTURAL",
            captured_at=datetime.now(UTC),
        )
    )
    await session.flush()

    event_type, applied = await apply_description_withdrawal(
        session, reinstatement, reviewer=_CHECKER, now=datetime.now(UTC)
    )

    assert (event_type, applied) == ("description.reinstatement.superseded.v1", False)
    assert await current_routine_description(session, routine.id) is None


async def test_the_api_admits_a_routine_subject_and_gates_it_on_the_datasource(
    session,
) -> None:
    """The one genuinely new authorization question this row raised. Every other
    description write gates `resource_type="table"`, and a routine has no table;
    this follows `ontology_api`'s own `ROUTINE` precedent and gates the
    datasource instead."""
    table, _ = await _seed(session)
    routine = await _seed_routine(session, table)
    await _describe_routine(session, routine)
    await session.commit()

    withdrawal = await create_description_withdrawal(
        DescriptionWithdrawalCreate(
            subject_type="ROUTINE",
            subject_id=routine.id,
            reason="It describes the wrong procedure.",
        ),
        _context(routine.organization_id, _MAKER),
        session,
        _SETTINGS,
    )

    assert (withdrawal.subject_type, withdrawal.status) == ("ROUTINE", "PENDING_REVIEW")
    gate_source = inspect.getsource(_authorize_subject)
    routine_branch = gate_source[gate_source.index('if body.subject_type == "ROUTINE"') :]
    assert 'resource_type="datasource"' in routine_branch.split("return")[0]
