"""Column description drafts: the GL-9 contract, one level down.

What has to hold however the drafting evolves -- the properties
`test_asset_description.py` pins for tables, plus the ones specific to columns:

(a) a low-evidence draft never reaches review: `ensure_reviewable` refuses it
    before any `GovernanceReview` exists;
(b) no score, however high, publishes without an independent decision -- the
    only publisher is called from the COLUMN_DESCRIPTION_DRAFT adapter alone,
    and neither its submitter nor anyone who edited it may approve it;
(c) nothing is read into a column's *name*: a column with no documentation gets
    a draft that says only what the catalog says, and it scores below the bar;
(d) a draft cannot overwrite a description it did not see (the version check),
    cannot outlive a workbook edit that described its column (supersede), and a
    workbook cannot be saved back into a datasource it was not exported from.

Pure functions are tested directly. Everything else runs against a real
SQLite engine through the endpoint functions and `decide_governance_review`,
the only route to publication, following `test_model_import.py`.
"""

from __future__ import annotations

import inspect
import itertools
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.reviewer_agent as reviewer_agent
import aida.semantic_api as semantic_api
from aida.asset_description_service import MINIMUM_EVIDENCE_FOR_REVIEW
from aida.column_description_api import (
    edit_column_description_draft,
    generate_column_description_drafts,
    list_column_description_drafts,
    submit_column_description_draft,
    submit_table_column_description_drafts,
)
from aida.column_description_service import (
    ColumnEvidence,
    compose_column_draft_text,
    score_column_evidence,
)
from aida.column_documentation import (
    current_descriptions_by_column_id,
    publish_column_description,
)
from aida.config import Settings
from aida.db import Base
from aida.main import app
from aida.model_export import COLUMN_SHEET, README_SHEET, compose_model_workbook
from aida.model_import import apply_model_import_batch, parse_and_diff_workbook
from aida.models import (
    AuditEvent,
    ColumnDescriptionDraft,
    ColumnDocumentationVersion,
    DataDomain,
    DataSource,
    DbtResource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.review_risk_tiers import TIER_T0, risk_tier_for
from aida.schemas import (
    ColumnDescriptionDraftEdit,
    ColumnDescriptionDraftGenerate,
    GovernanceDecisionRequest,
)
from aida.security import SecurityContext
from aida.semantic_api import decide_governance_review
from aida.xlsx import Sheet, write_workbook

_SETTINGS = Settings()
_STEWARD = "steward-a@example.com"
_EDITOR = "steward-b@example.com"
_REVIEWER = "reviewer@example.com"
_audit_event_ids = itertools.count(50_000_000)


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


def _context(organization_id: UUID, principal: str, *roles: str) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=organization_id,
        roles=frozenset(roles or ("DataSteward",)),
    )


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def _evidence(**overrides: object) -> ColumnEvidence:
    defaults: dict[str, object] = {
        "column_id": uuid4(),
        "table_id": uuid4(),
        "column_name": "amt_ccy",
        "table_name": "orders",
        "schema_name": "sales",
        "physical_type": "varchar(3)",
        "nullable": True,
        "classification": "UNCLASSIFIED",
        "source_description": None,
        "dbt_description": None,
        "primary_key_width": 0,
        "references": (),
        "related_to": (),
        "relationship_candidate_ids": (),
        "referenced_by": (),
        "current_description_version": None,
    }
    defaults.update(overrides)
    return ColumnEvidence(**defaults)  # type: ignore[arg-type]


def test_column_draft_api_contracts_are_exposed() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/organizations/{organization_id}/column-description-drafts/generate" in paths
    assert "/v1/organizations/{organization_id}/column-description-drafts" in paths
    assert "/v1/column-description-drafts/{draft_id}" in paths
    assert "/v1/column-description-drafts/{draft_id}/submit" in paths
    assert "/v1/tables/{table_id}/column-description-drafts/submit" in paths


def test_a_name_and_a_type_are_described_as_exactly_that_and_cannot_reach_review() -> None:
    """(c): `amt_ccy` is not guessed to be an ISO 4217 currency code."""
    evidence = _evidence()
    assert compose_column_draft_text(evidence) == (
        "amt_ccy is a column of sales.orders (varchar(3), nullable)."
    )
    assert score_column_evidence(evidence).overall < MINIMUM_EVIDENCE_FOR_REVIEW
    # Classification is a catalog fact too; it does not make a description.
    classified = _evidence(classification="INTERNAL")
    assert score_column_evidence(classified).overall < MINIMUM_EVIDENCE_FOR_REVIEW


def test_a_primary_key_alone_is_not_a_description() -> None:
    evidence = _evidence(column_name="order_id", primary_key_width=1, classification="INTERNAL")
    assert "It is the table's primary key." in compose_column_draft_text(evidence)
    assert score_column_evidence(evidence).overall < MINIMUM_EVIDENCE_FOR_REVIEW


@pytest.mark.parametrize(
    "authored",
    [
        {"source_description": "Order lifecycle state"},
        {"dbt_description": "The customer who placed the order"},
    ],
)
def test_any_authored_text_reaches_review(authored: dict[str, str]) -> None:
    assert score_column_evidence(_evidence(**authored)).overall >= MINIMUM_EVIDENCE_FOR_REVIEW


def test_structure_reaches_review_only_when_corroborated_from_two_places() -> None:
    declared_only = _evidence(references=("customers.customer_id",), classification="INTERNAL")
    corroborated = _evidence(
        references=("customers.customer_id",),
        related_to=("accounts.customer_id",),
        classification="INTERNAL",
    )
    assert score_column_evidence(declared_only).overall < MINIMUM_EVIDENCE_FOR_REVIEW
    assert score_column_evidence(corroborated).overall >= MINIMUM_EVIDENCE_FOR_REVIEW


def test_adding_evidence_never_lowers_any_dimension() -> None:
    bare = score_column_evidence(_evidence())
    additions: list[dict[str, object]] = [
        {"source_description": "comment"},
        {"dbt_description": "dbt text"},
        {"primary_key_width": 1},
        {"references": ("customers.customer_id",)},
        {"related_to": ("accounts.customer_id",)},
        {"referenced_by": ("payments",)},
        {"classification": "CONFIDENTIAL"},
    ]
    for addition in additions:
        richer = score_column_evidence(_evidence(**addition))
        for dimension in ("accuracy", "clarity", "style", "completeness", "overall"):
            assert getattr(richer, dimension) >= getattr(bare, dimension), (addition, dimension)


def test_scoring_is_deterministic() -> None:
    evidence = _evidence(dbt_description="x", references=("a.b",))
    assert score_column_evidence(evidence) == score_column_evidence(evidence)


def test_every_authored_sentence_says_where_it_came_from() -> None:
    text = compose_column_draft_text(
        _evidence(
            column_name="customer_id",
            physical_type="uuid",
            nullable=False,
            references=("customers.customer_id",),
            dbt_description="The customer who placed the order",
            source_description="FK to customers",
        )
    )
    assert text == (
        "customer_id is a column of sales.orders (uuid, not null). "
        "It references customers.customer_id. "
        "Its dbt definition describes it as: The customer who placed the order. "
        "The source system's comment on it reads: FK to customers."
    )


def test_a_source_comment_that_repeats_dbt_is_not_said_twice() -> None:
    text = compose_column_draft_text(
        _evidence(dbt_description="Order state.", source_description="order state")
    )
    assert text.count("Order state") + text.count("order state") == 1


def test_long_relationship_lists_are_summarised() -> None:
    text = compose_column_draft_text(
        _evidence(referenced_by=("payments", "refunds", "shipments", "invoices", "returns"))
    )
    assert (
        "Foreign keys on the tables payments, refunds, shipments and 2 more reference it." in text
    )


def test_apply_column_description_draft_has_exactly_one_call_site() -> None:
    """(b): the only publisher is reached only through the decision registry."""
    source = inspect.getsource(semantic_api)
    assert len(re.findall(r"apply_column_description_draft\(", source)) == 1
    adapter = inspect.getsource(semantic_api._decide_column_description_draft)
    assert "apply_column_description_draft(" in adapter
    assert "reject_column_description_draft(" in adapter
    assert (
        semantic_api._TARGET_EFFECT_ADAPTERS["COLUMN_DESCRIPTION_DRAFT"]
        is semantic_api._decide_column_description_draft
    )
    assert "apply_column_description_draft(" not in inspect.getsource(
        semantic_api.decide_governance_review
    )


def test_column_drafts_are_language_tier_and_agent_judgeable_on_their_own_score() -> None:
    assert risk_tier_for("COLUMN_DESCRIPTION_DRAFT") == TIER_T0
    assert "COLUMN_DESCRIPTION_DRAFT" in reviewer_agent._EVIDENCE_RESOLVERS


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


class _Estate:
    """The seeded catalog, with identifiers copied out as plain values.

    A test that rolls back after an expected refusal expires every ORM
    instance in the session, and reading an expired attribute from async code
    is `MissingGreenlet` -- an error about the test harness, masking whatever
    the test was checking. Tests that roll back use only these plain ids.
    """

    def __init__(
        self,
        datasource: DataSource,
        customers: MetadataTable,
        orders: MetadataTable,
        columns: dict[str, MetadataColumn],
    ) -> None:
        self.datasource = datasource
        self.customers = customers
        self.orders = orders
        self.org_id: UUID = datasource.organization_id
        self.orders_id: UUID = orders.id
        self.column_ids: dict[str, UUID] = {name: column.id for name, column in columns.items()}


@dataclass(frozen=True)
class _DraftRef:
    """What the tests need from a draft, detached from the session."""

    id: UUID
    table_id: UUID
    column_id: UUID
    drafted_text: str


async def _seed(session: AsyncSession) -> _Estate:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Retail", code=f"RTL{uuid4().hex[:6]}"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Retail",
        code=f"RET{uuid4().hex[:6]}",
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
    datasource = _datasource(org.id, lob.id, domain.id, project.id, name="warehouse")
    session.add(datasource)
    await session.flush()
    catalog = MetadataCatalog(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id, name="wh", fingerprint="fp"
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="sales", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()
    customers = _table(org.id, datasource.id, schema.id, "customers")
    orders = _table(org.id, datasource.id, schema.id, "orders")
    session.add_all([customers, orders])
    await session.flush()
    columns = {
        "customers.customer_id": _column(org.id, customers.id, "customer_id", 0, "uuid", False),
        "order_id": _column(org.id, orders.id, "order_id", 0, "bigint", False),
        "customer_id": _column(
            org.id, orders.id, "customer_id", 1, "uuid", False, comment="FK to customers"
        ),
        "amt_ccy": _column(org.id, orders.id, "amt_ccy", 2, "varchar(3)", True),
        "status": _column(
            org.id, orders.id, "status", 3, "varchar(20)", True, comment="Order lifecycle state"
        ),
    }
    session.add_all(list(columns.values()))
    await session.flush()
    session.add_all(
        [
            _constraint(
                org.id, datasource.id, customers.id, "customers_pk", "PRIMARY_KEY", ["customer_id"]
            ),
            _constraint(org.id, datasource.id, orders.id, "orders_pk", "PRIMARY_KEY", ["order_id"]),
            _constraint(
                org.id,
                datasource.id,
                orders.id,
                "orders_customer_fk",
                "FOREIGN_KEY",
                ["customer_id"],
                referenced_table_id=customers.id,
                referenced_columns=["customer_id"],
            ),
            # `artifact_import_id` points at nothing: SQLite does not enforce the
            # foreign key, and this test is about the column text dbt carries,
            # not about dbt import bookkeeping.
            DbtResource(
                id=uuid4(),
                organization_id=org.id,
                artifact_import_id=uuid4(),
                unique_id="model.shop.orders",
                resource_type="model",
                package_name="shop",
                name="orders",
                sql_parse_status="PARSED",
                column_descriptions={"customer_id": "The customer who placed the order"},
                matched_table_id=orders.id,
            ),
        ]
    )
    await session.flush()
    await session.commit()
    return _Estate(datasource, customers, orders, columns)


def _datasource(
    org_id: UUID, lob_id: UUID, domain_id: UUID, project_id: UUID, *, name: str
) -> DataSource:
    return DataSource(
        id=uuid4(),
        organization_id=org_id,
        line_of_business_id=lob_id,
        data_domain_id=domain_id,
        project_id=project_id,
        name=name,
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://X",
        capabilities={},
    )


def _table(org_id: UUID, datasource_id: UUID, schema_id: UUID, name: str) -> MetadataTable:
    return MetadataTable(
        id=uuid4(),
        organization_id=org_id,
        datasource_id=datasource_id,
        schema_id=schema_id,
        name=name,
        object_type="BASE_TABLE",
        status="ACTIVE",
        fingerprint="fp",
    )


def _column(
    org_id: UUID,
    table_id: UUID,
    name: str,
    position: int,
    physical_type: str,
    nullable: bool,
    *,
    comment: str | None = None,
) -> MetadataColumn:
    return MetadataColumn(
        id=uuid4(),
        organization_id=org_id,
        table_id=table_id,
        name=name,
        ordinal_position=position,
        physical_type=physical_type,
        nullable=nullable,
        source_description=comment,
        status="ACTIVE",
        fingerprint="fp",
    )


def _constraint(
    org_id: UUID,
    datasource_id: UUID,
    table_id: UUID,
    name: str,
    constraint_type: str,
    columns: list[str],
    *,
    referenced_table_id: UUID | None = None,
    referenced_columns: list[str] | None = None,
) -> MetadataConstraint:
    return MetadataConstraint(
        id=uuid4(),
        organization_id=org_id,
        datasource_id=datasource_id,
        table_id=table_id,
        name=name,
        constraint_type=constraint_type,
        columns=columns,
        referenced_table_id=referenced_table_id,
        referenced_columns=referenced_columns or [],
        status="ACTIVE",
        fingerprint="fp",
    )


async def _generate(session: AsyncSession, estate: _Estate, *, include_described: bool = False):
    return await generate_column_description_drafts(
        estate.org_id,
        ColumnDescriptionDraftGenerate(
            table_ids=[estate.orders_id], include_described=include_described
        ),
        _context(estate.org_id, _STEWARD),
        session,
        _SETTINGS,
    )


async def _open_draft(session: AsyncSession, column_id: UUID) -> _DraftRef:
    draft = await session.scalar(
        select(ColumnDescriptionDraft).where(
            ColumnDescriptionDraft.column_id == column_id,
            ColumnDescriptionDraft.status.in_(("DRAFT", "PENDING_APPROVAL")),
        )
    )
    assert draft is not None
    return _DraftRef(draft.id, draft.table_id, draft.column_id, draft.drafted_text)


async def _submit(session: AsyncSession, estate: _Estate, draft_id: UUID) -> UUID:
    review = await submit_column_description_draft(
        draft_id, _context(estate.org_id, _STEWARD), session, _SETTINGS
    )
    return review.id


async def _decide(
    session: AsyncSession, estate: _Estate, review_id: UUID, principal: str, decision: str
):
    reason = None if decision == "APPROVE" else "not accurate"
    return await decide_governance_review(
        review_id,
        GovernanceDecisionRequest(decision=decision, reason=reason),
        _context(estate.org_id, principal, "Reviewer", "DataSteward"),
        session,
    )


async def test_generation_drafts_undescribed_columns_from_catalog_evidence(
    session: AsyncSession,
) -> None:
    estate = await _seed(session)
    result = await _generate(session, estate)

    drafted = {draft.column_name: draft for draft in result.drafts}
    assert set(drafted) == {"order_id", "customer_id", "amt_ccy", "status"}
    assert drafted["customer_id"].drafted_text == (
        "customer_id is a column of sales.orders (uuid, not null). "
        "It references customers.customer_id. "
        "Its dbt definition describes it as: The customer who placed the order. "
        "The source system's comment on it reads: FK to customers."
    )
    assert drafted["customer_id"].reviewable is True
    assert drafted["status"].reviewable is True
    # Thin columns are drafted -- the steward can see exactly how little there
    # is -- but cannot be submitted.
    assert drafted["amt_ccy"].reviewable is False
    assert drafted["order_id"].reviewable is False
    assert result.below_review_threshold == 2
    assert result.created == 4
    assert result.tables_skipped == 0


async def test_generation_skips_described_retired_and_already_open_columns(
    session: AsyncSession,
) -> None:
    estate = await _seed(session)
    now = datetime.now(UTC)
    await publish_column_description(
        session,
        organization_id=estate.org_id,
        table_id=estate.orders_id,
        column_id=estate.column_ids["status"],
        description="Where the order is in its lifecycle.",
        created_by=_STEWARD,
        approved_by=_REVIEWER,
        approved_at=now,
    )
    retired = await publish_column_description(
        session,
        organization_id=estate.org_id,
        table_id=estate.orders_id,
        column_id=estate.column_ids["amt_ccy"],
        description="Currency of the amount.",
        created_by=_STEWARD,
        approved_by=_REVIEWER,
        approved_at=now,
    )
    retired.status = "WITHDRAWN"
    await session.commit()

    first = await _generate(session, estate)
    assert {draft.column_name for draft in first.drafts} == {"order_id", "customer_id"}
    assert first.skipped_described == 2  # one described, one deliberately retired

    second = await _generate(session, estate)
    assert second.created == 0
    assert second.skipped_open == 2

    replacements = await _generate(session, estate, include_described=True)
    assert {draft.column_name for draft in replacements.drafts} == {"status", "amt_ccy"}


async def test_the_database_allows_one_open_draft_per_column(session: AsyncSession) -> None:
    estate = await _seed(session)
    await _generate(session, estate)
    existing = await _open_draft(session, estate.column_ids["customer_id"])
    session.add(
        ColumnDescriptionDraft(
            organization_id=estate.org_id,
            table_id=existing.table_id,
            column_id=existing.column_id,
            drafted_text="A second, competing draft.",
            text_fingerprint="0" * 64,
            accuracy_score=1.0,
            clarity_score=1.0,
            style_score=1.0,
            completeness_score=1.0,
            overall_score=1.0,
            evidence={},
            status="DRAFT",
            created_by=_EDITOR,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_a_draft_below_the_evidence_bar_cannot_be_submitted(session: AsyncSession) -> None:
    estate = await _seed(session)
    await _generate(session, estate)
    thin = await _open_draft(session, estate.column_ids["amt_ccy"])
    with pytest.raises(HTTPException) as refused:
        await submit_column_description_draft(
            thin.id, _context(estate.org_id, _STEWARD), session, _SETTINGS
        )
    assert refused.value.status_code == 422
    await session.rollback()

    bulk = await submit_table_column_description_drafts(
        estate.orders_id, _context(estate.org_id, _STEWARD), session, _SETTINGS
    )
    assert len(bulk.submitted_review_ids) == 2  # customer_id and status
    assert bulk.skipped_below_threshold == 2  # amt_ccy and order_id


async def test_approval_needs_an_independent_reviewer_and_then_publishes(
    session: AsyncSession,
) -> None:
    estate = await _seed(session)
    await _generate(session, estate)
    draft = await _open_draft(session, estate.column_ids["customer_id"])
    review_id = await _submit(session, estate, draft.id)

    with pytest.raises(HTTPException) as self_approval:
        await _decide(session, estate, review_id, _STEWARD, "APPROVE")
    assert self_approval.value.status_code in (403, 409)
    await session.rollback()

    await _decide(session, estate, review_id, _REVIEWER, "APPROVE")
    published = await current_descriptions_by_column_id(session, [estate.column_ids["customer_id"]])
    version = published[estate.column_ids["customer_id"]]
    approved = await session.get(ColumnDescriptionDraft, draft.id)
    assert approved is not None
    await session.refresh(approved)
    assert version.description == approved.drafted_text
    assert approved.status == "APPROVED"
    assert approved.published_version_id == version.id


async def test_an_editor_cannot_approve_their_own_edit(session: AsyncSession) -> None:
    estate = await _seed(session)
    await _generate(session, estate)
    draft = await _open_draft(session, estate.column_ids["status"])
    fixed = "The order's lifecycle state: placed, paid, shipped or cancelled."
    await edit_column_description_draft(
        draft.id,
        ColumnDescriptionDraftEdit(drafted_text=fixed, expected_text=draft.drafted_text),
        _context(estate.org_id, _EDITOR),
        session,
        _SETTINGS,
    )
    review_id = await _submit(session, estate, draft.id)

    with pytest.raises(HTTPException) as editor_approval:
        await _decide(session, estate, review_id, _EDITOR, "APPROVE")
    assert "editor cannot approve" in str(editor_approval.value.detail)
    await session.rollback()

    await _decide(session, estate, review_id, _REVIEWER, "APPROVE")
    published = await current_descriptions_by_column_id(session, [estate.column_ids["status"]])
    assert published[estate.column_ids["status"]].description == fixed


async def test_an_edit_against_stale_text_is_refused(session: AsyncSession) -> None:
    estate = await _seed(session)
    await _generate(session, estate)
    draft = await _open_draft(session, estate.column_ids["status"])
    with pytest.raises(HTTPException) as stale:
        await edit_column_description_draft(
            draft.id,
            ColumnDescriptionDraftEdit(
                drafted_text="Someone's rewrite of the draft.", expected_text="not what it says"
            ),
            _context(estate.org_id, _EDITOR),
            session,
            _SETTINGS,
        )
    assert stale.value.status_code == 409


async def test_a_draft_cannot_replace_a_description_published_after_it_was_composed(
    session: AsyncSession,
) -> None:
    """(d): the lost-update rule. The draft was composed against 'no
    description'; someone published one since; approving must not replace it."""
    estate = await _seed(session)
    await _generate(session, estate)
    draft = await _open_draft(session, estate.column_ids["customer_id"])
    review_id = await _submit(session, estate, draft.id)
    await publish_column_description(
        session,
        organization_id=estate.org_id,
        table_id=estate.orders_id,
        column_id=estate.column_ids["customer_id"],
        description="Written by hand, through another path, after the draft.",
        created_by=_EDITOR,
        approved_by=_REVIEWER,
        approved_at=datetime.now(UTC),
    )
    await session.commit()

    with pytest.raises(HTTPException) as moved:
        await _decide(session, estate, review_id, _REVIEWER, "APPROVE")
    assert moved.value.status_code == 409
    assert "changed after the draft was composed" in str(moved.value.detail)
    await session.rollback()

    current = await current_descriptions_by_column_id(session, [estate.column_ids["customer_id"]])
    assert current[estate.column_ids["customer_id"]].description.startswith("Written by hand")
    still_pending = await session.scalar(
        select(ColumnDescriptionDraft.status).where(ColumnDescriptionDraft.id == draft.id)
    )
    assert still_pending == "PENDING_APPROVAL"


async def test_a_rejected_draft_is_not_proposed_again(session: AsyncSession) -> None:
    estate = await _seed(session)
    await _generate(session, estate)
    draft = await _open_draft(session, estate.column_ids["customer_id"])
    review_id = await _submit(session, estate, draft.id)
    await _decide(session, estate, review_id, _REVIEWER, "REJECT")

    again = await _generate(session, estate)
    assert again.skipped_duplicate_rejected == 1
    assert "customer_id" not in {draft.column_name for draft in again.drafts}


async def test_an_organization_wide_list_needs_a_steward_or_reviewer(
    session: AsyncSession,
) -> None:
    estate = await _seed(session)
    await _generate(session, estate)
    with pytest.raises(HTTPException) as refused:
        await list_column_description_drafts(
            estate.org_id,
            draft_status=None,
            table_id=None,
            limit=200,
            offset=0,
            context=_context(estate.org_id, "analyst@example.com", "Analyst"),
            session=session,
            settings=_SETTINGS,
        )
    assert refused.value.status_code == 403

    page = await list_column_description_drafts(
        estate.org_id,
        draft_status=None,
        table_id=estate.orders_id,
        limit=200,
        offset=0,
        context=_context(estate.org_id, _STEWARD),
        session=session,
        settings=_SETTINGS,
    )
    assert [item.column_name for item in page.items] == [
        "order_id",
        "customer_id",
        "amt_ccy",
        "status",
    ]


def _columns_sheet(sheets: list[Sheet]) -> Sheet:
    return next(sheet for sheet in sheets if sheet.name == COLUMN_SHEET)


def _with_cell(sheets: list[Sheet], column_id: UUID, field: str, value: str) -> list[Sheet]:
    edited: list[Sheet] = []
    for sheet in sheets:
        if sheet.name == COLUMN_SHEET:
            id_index = sheet.headers.index("column_id")
            field_index = sheet.headers.index(field)
            rows = [list(row) for row in sheet.rows]
            for row in rows:
                if row[id_index] == str(column_id):
                    row[field_index] = value
            sheet = Sheet(name=sheet.name, headers=sheet.headers, rows=rows)
        edited.append(sheet)
    return edited


async def test_the_workbook_carries_the_open_draft_beside_business_description(
    session: AsyncSession,
) -> None:
    estate = await _seed(session)
    await _generate(session, estate)
    draft = await _open_draft(session, estate.column_ids["customer_id"])
    composition = await compose_model_workbook(
        session, datasource=estate.datasource, generated_at=datetime.now(UTC), generated_by=_STEWARD
    )
    sheet = _columns_sheet(composition.sheets)
    headers = sheet.headers
    assert headers.index("drafted_description") == headers.index("business_description") + 1
    row = next(
        dict(zip(headers, values, strict=True))
        for values in sheet.rows
        if values[headers.index("column_id")] == str(estate.column_ids["customer_id"])
    )
    assert row["drafted_description"] == draft.drafted_text
    assert row["draft_status"] == "DRAFT"
    assert row["draft_id"] == str(draft.id)
    readme = next(sheet for sheet in composition.sheets if sheet.name == README_SHEET)
    assert any(values[0] == "drafted_description" for values in readme.rows)


async def test_a_workbook_edit_that_describes_a_column_supersedes_its_draft(
    session: AsyncSession,
) -> None:
    estate = await _seed(session)
    await _generate(session, estate)
    composition = await compose_model_workbook(
        session, datasource=estate.datasource, generated_at=datetime.now(UTC), generated_by=_STEWARD
    )
    content = write_workbook(
        _with_cell(
            composition.sheets,
            estate.column_ids["customer_id"],
            "business_description",
            "The customer who placed the order; references customers.",
        )
    )
    batch = await parse_and_diff_workbook(
        session,
        datasource=estate.datasource,
        content=content,
        filename="model.xlsx",
        uploaded_by=_STEWARD,
    )
    assert batch.change_count == 1
    batch.status = "PENDING_REVIEW"  # submission is not what this test is about
    await apply_model_import_batch(session, batch, reviewer=_REVIEWER, now=datetime.now(UTC))

    statuses = dict(
        (
            await session.execute(
                select(MetadataColumn.name, ColumnDescriptionDraft.status)
                .join(MetadataColumn, MetadataColumn.id == ColumnDescriptionDraft.column_id)
                .where(ColumnDescriptionDraft.table_id == estate.orders_id)
            )
        ).all()
    )
    assert statuses["customer_id"] == "SUPERSEDED"
    assert statuses["status"] == "DRAFT"
    assert statuses["amt_ccy"] == "DRAFT"


async def test_a_workbook_cannot_be_saved_into_a_datasource_it_was_not_exported_from(
    session: AsyncSession,
) -> None:
    estate = await _seed(session)
    other = _datasource(
        estate.org_id,
        estate.datasource.line_of_business_id,
        estate.datasource.data_domain_id,
        estate.datasource.project_id,
        name="other-warehouse",
    )
    session.add(other)
    await session.commit()
    composition = await compose_model_workbook(
        session, datasource=estate.datasource, generated_at=datetime.now(UTC), generated_by=_STEWARD
    )
    content = write_workbook(composition.sheets)

    with pytest.raises(HTTPException) as refused:
        await parse_and_diff_workbook(
            session, datasource=other, content=content, filename="m.xlsx", uploaded_by=_STEWARD
        )
    assert refused.value.status_code == 422
    assert "different datasource" in str(refused.value.detail)

    # Into its own source it is accepted.
    await parse_and_diff_workbook(
        session,
        datasource=estate.datasource,
        content=content,
        filename="m.xlsx",
        uploaded_by=_STEWARD,
    )


async def test_a_workbook_with_no_readme_id_still_falls_back_to_the_per_row_check(
    session: AsyncSession,
) -> None:
    """An old export, or a hand-rebuilt file, has no identity row. It is not
    refused wholesale -- the per-row id check was always the authority."""
    estate = await _seed(session)
    other = _datasource(
        estate.org_id,
        estate.datasource.line_of_business_id,
        estate.datasource.data_domain_id,
        estate.datasource.project_id,
        name="other-warehouse",
    )
    session.add(other)
    await session.commit()
    composition = await compose_model_workbook(
        session, datasource=estate.datasource, generated_at=datetime.now(UTC), generated_by=_STEWARD
    )
    content = write_workbook([sheet for sheet in composition.sheets if sheet.name != README_SHEET])
    batch = await parse_and_diff_workbook(
        session, datasource=other, content=content, filename="m.xlsx", uploaded_by=_STEWARD
    )
    assert batch.change_count == 0
    assert batch.rejected_row_count > 0


def test_documentation_version_rows_are_the_published_store() -> None:
    """A guard on the fixture above: `retired.status = "WITHDRAWN"` is only a
    meaningful retirement if withdrawal is a status on this row."""
    assert "status" in ColumnDocumentationVersion.__table__.columns
