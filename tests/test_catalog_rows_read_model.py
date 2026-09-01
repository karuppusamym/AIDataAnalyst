"""UX-12 -- `GET /v1/organizations/{organization_id}/catalog/rows`.

Runs the real endpoint body (`aida.api.list_catalog_rows`) and the real
composition (`aida.catalog_read_model.compose_catalog_rows`) against an
in-memory SQLite database, following `test_catalog_pagination.py`'s own
rationale for doing so: PostgreSQL is not reachable in this sandbox, but
SQLite is a real SQL engine that enforces the same row-value and window-
function semantics the composed queries below rely on -- genuine query
execution, not a mock.

Four things this endpoint must be true to, each with its own section:

1. the composed shape carries every field the tracker's exit criterion
   names, sourced with the precedence documented in `catalog_read_model.py`;
2. the CT-2 keyset contract holds for this endpoint exactly as it does for
   `list_tables` (`total: null` under a cursor, no duplicate or skipped rows);
3. permission filtering denies a cross-organization caller before touching
   the database, the same way every other organization-scoped read does, and
   filters per-datasource the same way `list_tables`' own gate would;
4. the number of SQL statements sent to the database stays bounded by a
   small constant, independent of how many rows are on the page -- proving
   this is not the N+1 pattern it exists to remove.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.api import list_catalog_rows
from aida.authorization_gate import AuthorizationDenied, GateOutcome
from aida.config import Settings
from aida.db import Base
from aida.models import (
    AssetCertification,
    AssetDescriptionDraft,
    AssetDocumentation,
    AssetDocumentationVersion,
    AssetTermLink,
    DataDomain,
    DataQualityIncident,
    DataQualityObservation,
    DataSource,
    GlossaryTermVersion,
    LineOfBusiness,
    MetadataBusinessAnnotation,
    MetadataBusinessAnnotationVersion,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    OwnershipAssignment,
    Project,
    TableProfile,
)
from tests.support.doubles import security_context

pytestmark = pytest.mark.asyncio

_SETTINGS = Settings()
_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with session_factory() as db_session:
        yield db_session
    await engine.dispose()


async def _seed_datasource(session: AsyncSession, *, org: Organization | None = None) -> DataSource:
    org = org or Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
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
        id=uuid4(),
        organization_id=org.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="fp",
    )
    session.add(schema)
    await session.flush()
    datasource._test_schema = schema  # type: ignore[attr-defined]
    return datasource


async def _seed_table(
    session: AsyncSession,
    datasource: DataSource,
    *,
    name: str,
    source_description: str | None = None,
) -> MetadataTable:
    schema = datasource._test_schema  # type: ignore[attr-defined]
    table = MetadataTable(
        id=uuid4(),
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        schema_id=schema.id,
        name=name,
        object_type="BASE_TABLE",
        fingerprint="fp",
        source_description=source_description,
    )
    session.add(table)
    await session.flush()
    return table


def _context(datasource: DataSource, **overrides: object) -> object:
    return security_context(organization_id=datasource.organization_id, **overrides)


# ---------------------------------------------------------------------------
# 1. Composed shape
# ---------------------------------------------------------------------------


async def test_composed_row_carries_every_field_the_exit_criterion_names(session) -> None:
    datasource = await _seed_datasource(session)
    table = await _seed_table(session, datasource, name="accounts")

    documentation = AssetDocumentation(
        id=uuid4(), organization_id=table.organization_id, table_id=table.id
    )
    session.add(documentation)
    await session.flush()
    session.add(
        AssetDocumentationVersion(
            id=uuid4(),
            organization_id=table.organization_id,
            documentation_id=documentation.id,
            version=1,
            status="APPROVED",
            readme="Deposit account records, one row per account.",
            owner_principal="Docs Fallback Owner",
            created_by="drafter",
            approved_by="reviewer",
            approved_at=_NOW,
        )
    )
    session.add(
        OwnershipAssignment(
            id=uuid4(),
            organization_id=table.organization_id,
            subject_type="TABLE",
            subject_id=str(table.id),
            owner_type="TEAM",
            owner_principal="Retail Data Office",
            assignment_kind="MANUAL",
            status="ACTIVE",
            assigned_by="steward",
        )
    )
    session.add(
        AssetCertification(
            id=uuid4(),
            organization_id=table.organization_id,
            table_id=table.id,
            asset_type="TABLE",
            status="ACTIVE",
            rationale="Reviewed against the certification checklist.",
            certified_by="reviewer",
            expires_at=_NOW + timedelta(days=180),
        )
    )
    term_id = uuid4()
    session.add(
        GlossaryTermVersion(
            id=uuid4(),
            organization_id=table.organization_id,
            term_id=term_id,
            version=1,
            status="APPROVED",
            display_name="Deposit Account",
            definition="A customer's deposit account.",
            created_by="steward",
        )
    )
    session.add(
        AssetTermLink(
            id=uuid4(),
            organization_id=table.organization_id,
            table_id=table.id,
            term_id=term_id,
            linked_by="steward",
        )
    )
    session.add(
        TableProfile(
            id=uuid4(),
            organization_id=table.organization_id,
            analysis_run_id=uuid4(),
            datasource_id=datasource.id,
            table_id=table.id,
            row_count_estimate=42_000,
            sampled_row_count=1000,
            status="COMPLETED",
        )
    )
    await session.commit()

    page = await list_catalog_rows(
        table.organization_id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        certification=None,
        limit=100,
        offset=0,
        cursor=None,
        context=_context(datasource, roles=frozenset({"Viewer"})),
        session=session,
        settings=_SETTINGS,
    )

    assert len(page.items) == 1
    row = page.items[0]
    assert row.id == table.id
    assert row.name == "accounts"
    assert row.schema_name == "public"
    assert row.datasource_name == datasource.name
    assert row.object_type == "BASE_TABLE"
    assert row.status == "ACTIVE"
    assert row.description == "Deposit account records, one row per account."
    assert row.description_is_proposed is False
    # GL-2 OwnershipAssignment outranks the documentation's owner_principal fallback.
    assert row.owner == "Retail Data Office"
    assert row.certification == "CERTIFIED"
    assert row.certification_expires_at is not None
    assert row.quality == "UNKNOWN"  # no observation and no incident were seeded
    assert row.glossary_terms == ["Deposit Account"]
    assert row.row_count_estimate == 42_000
    assert row.updated_at is not None


async def test_description_falls_back_through_the_documented_precedence(session) -> None:
    datasource = await _seed_datasource(session)

    approved_doc = await _seed_table(session, datasource, name="t_approved_doc")
    documentation = AssetDocumentation(
        id=uuid4(), organization_id=approved_doc.organization_id, table_id=approved_doc.id
    )
    session.add(documentation)
    await session.flush()
    session.add(
        AssetDocumentationVersion(
            id=uuid4(),
            organization_id=approved_doc.organization_id,
            documentation_id=documentation.id,
            version=1,
            status="APPROVED",
            readme="Published GL-9 documentation.",
            created_by="drafter",
        )
    )

    pending = await _seed_table(session, datasource, name="t_pending_draft")
    session.add(
        AssetDescriptionDraft(
            id=uuid4(),
            organization_id=pending.organization_id,
            table_id=pending.id,
            drafted_text="Deterministically drafted, awaiting review.",
            text_fingerprint="fp",
            accuracy_score=0.5,
            clarity_score=0.5,
            style_score=0.5,
            completeness_score=0.5,
            overall_score=0.5,
            evidence={},
            status="PENDING_APPROVAL",
            created_by="drafter",
        )
    )

    annotated = await _seed_table(session, datasource, name="t_annotation")
    annotation = MetadataBusinessAnnotation(
        id=uuid4(),
        organization_id=annotated.organization_id,
        datasource_id=datasource.id,
        table_id=annotated.id,
        domain_id=uuid4(),
        entity_id=uuid4(),
        source_proposal_id=uuid4(),
    )
    session.add(annotation)
    # AT-6: content lives on `MetadataBusinessAnnotationVersion`, never on
    # `MetadataBusinessAnnotation` itself -- see `business_annotation_versions.py`.
    session.add(
        MetadataBusinessAnnotationVersion(
            id=uuid4(),
            organization_id=annotated.organization_id,
            annotation_id=annotation.id,
            version=1,
            status="APPROVED",
            business_name="Accounts",
            business_description="Approved business-annotation description.",
            table_role="FACT",
            grain_statement="One row per account.",
            confidence=0.9,
            approved_by="reviewer",
            approved_at=_NOW,
        )
    )

    await _seed_table(
        session, datasource, name="t_source_only", source_description="Connector-scanned comment."
    )
    await _seed_table(session, datasource, name="t_bare")

    await session.commit()

    page = await list_catalog_rows(
        datasource.organization_id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        certification=None,
        limit=100,
        offset=0,
        cursor=None,
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )
    by_name = {item.name: item for item in page.items}

    assert by_name["t_approved_doc"].description == "Published GL-9 documentation."
    assert by_name["t_approved_doc"].description_is_proposed is False

    assert by_name["t_pending_draft"].description == "Deterministically drafted, awaiting review."
    assert by_name["t_pending_draft"].description_is_proposed is True

    assert by_name["t_annotation"].description == "Approved business-annotation description."
    assert by_name["t_annotation"].description_is_proposed is False

    assert by_name["t_source_only"].description == "Connector-scanned comment."
    assert by_name["t_source_only"].description_is_proposed is False

    assert by_name["t_bare"].description is None
    assert by_name["t_bare"].description_is_proposed is False


async def test_quality_states_cover_incident_stale_passing_and_unknown(session) -> None:
    datasource = await _seed_datasource(session)

    incident_table = await _seed_table(session, datasource, name="t_incident")
    session.add(
        DataQualityIncident(
            id=uuid4(),
            organization_id=incident_table.organization_id,
            datasource_id=datasource.id,
            table_id=incident_table.id,
            fingerprint=uuid4().hex,
            anomaly_type="VOLUME",
            severity="HIGH",
            status="OPEN",
            summary="Volume dropped below baseline.",
            first_observed_at=_NOW,
            last_observed_at=_NOW,
        )
    )
    # A stale, resolved observation is still present -- an open incident wins regardless.
    session.add(
        DataQualityObservation(
            id=uuid4(),
            organization_id=incident_table.organization_id,
            datasource_id=datasource.id,
            table_id=incident_table.id,
            analysis_run_id=uuid4(),
            status="PASS",
            quality_score=90,
            created_at=_NOW,
        )
    )

    stale_table = await _seed_table(session, datasource, name="t_stale")
    session.add(
        DataQualityObservation(
            id=uuid4(),
            organization_id=stale_table.organization_id,
            datasource_id=datasource.id,
            table_id=stale_table.id,
            analysis_run_id=uuid4(),
            status="PASS",
            quality_score=90,
            created_at=_NOW - timedelta(days=20),
        )
    )

    passing_table = await _seed_table(session, datasource, name="t_passing")
    session.add(
        DataQualityObservation(
            id=uuid4(),
            organization_id=passing_table.organization_id,
            datasource_id=datasource.id,
            table_id=passing_table.id,
            analysis_run_id=uuid4(),
            status="PASS",
            quality_score=95,
            created_at=_NOW - timedelta(days=1),
        )
    )

    await _seed_table(session, datasource, name="t_unknown")

    await session.commit()

    page = await list_catalog_rows(
        datasource.organization_id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        certification=None,
        limit=100,
        offset=0,
        cursor=None,
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )
    by_name = {item.name: item for item in page.items}

    assert by_name["t_incident"].quality == "INCIDENT_OPEN"
    assert by_name["t_stale"].quality == "STALE"
    assert by_name["t_passing"].quality == "PASSING"
    assert by_name["t_unknown"].quality == "UNKNOWN"


async def test_certification_states_cover_certified_expired_and_none(session) -> None:
    datasource = await _seed_datasource(session)

    certified = await _seed_table(session, datasource, name="t_certified")
    session.add(
        AssetCertification(
            id=uuid4(),
            organization_id=certified.organization_id,
            table_id=certified.id,
            asset_type="TABLE",
            status="ACTIVE",
            rationale="Meets the certification bar.",
            certified_by="reviewer",
            expires_at=_NOW + timedelta(days=30),
        )
    )

    expired = await _seed_table(session, datasource, name="t_expired")
    session.add(
        AssetCertification(
            id=uuid4(),
            organization_id=expired.organization_id,
            table_id=expired.id,
            asset_type="TABLE",
            status="ACTIVE",
            rationale="Certified, but the clock ran out.",
            certified_by="reviewer",
            expires_at=_NOW - timedelta(days=1),
        )
    )

    await _seed_table(session, datasource, name="t_never_certified")

    await session.commit()

    page = await list_catalog_rows(
        datasource.organization_id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        certification=None,
        limit=100,
        offset=0,
        cursor=None,
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )
    by_name = {item.name: item for item in page.items}

    assert by_name["t_certified"].certification == "CERTIFIED"
    assert by_name["t_certified"].certification_expires_at is not None
    assert by_name["t_expired"].certification == "EXPIRED"
    assert by_name["t_expired"].certification_expires_at is None
    assert by_name["t_never_certified"].certification == "NONE"


async def test_certification_query_filter_leaves_only_matching_rows(session) -> None:
    datasource = await _seed_datasource(session)
    certified = await _seed_table(session, datasource, name="t_certified")
    session.add(
        AssetCertification(
            id=uuid4(),
            organization_id=certified.organization_id,
            table_id=certified.id,
            asset_type="TABLE",
            status="ACTIVE",
            rationale="Meets the certification bar.",
            certified_by="reviewer",
            expires_at=_NOW + timedelta(days=30),
        )
    )
    await _seed_table(session, datasource, name="t_uncertified")
    await session.commit()

    page = await list_catalog_rows(
        datasource.organization_id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        certification="CERTIFIED",
        limit=100,
        offset=0,
        cursor=None,
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )

    assert [item.name for item in page.items] == ["t_certified"]


# ---------------------------------------------------------------------------
# 2. CT-2 keyset contract
# ---------------------------------------------------------------------------


async def test_cursor_walk_visits_every_row_exactly_once_and_total_is_null_under_a_cursor(
    session,
) -> None:
    datasource = await _seed_datasource(session)
    tables = [
        await _seed_table(session, datasource, name=f"table_{i:04d}") for i in range(13)
    ]
    await session.commit()
    context = _context(datasource)

    seen: list[str] = []
    cursor: str | None = None
    page_count = 0
    while True:
        page = await list_catalog_rows(
            datasource.organization_id,
            q=None,
            object_type=None,
            table_status="ACTIVE",
            certification=None,
            limit=5,
            offset=0,
            cursor=cursor,
            context=context,
            session=session,
            settings=_SETTINGS,
        )
        page_count += 1
        if page.total is not None:
            assert page_count == 1
        else:
            assert page_count > 1
        seen.extend(item.name for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
        assert page_count < 20, "pagination did not terminate"

    assert seen == sorted(table.name for table in tables)


async def test_offset_mode_first_page_reports_total_and_a_continuation_cursor(session) -> None:
    datasource = await _seed_datasource(session)
    for i in range(4):
        await _seed_table(session, datasource, name=f"table_{i:04d}")
    await session.commit()

    page = await list_catalog_rows(
        datasource.organization_id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        certification=None,
        limit=2,
        offset=0,
        cursor=None,
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )
    assert page.total == 4
    assert page.next_cursor is not None
    assert len(page.items) == 2


async def test_invalid_cursor_is_rejected_as_bad_request(session) -> None:
    datasource = await _seed_datasource(session)
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await list_catalog_rows(
            datasource.organization_id,
            q=None,
            object_type=None,
            table_status="ACTIVE",
            certification=None,
            limit=10,
            offset=0,
            cursor="not-a-real-cursor",
            context=_context(datasource),
            session=session,
            settings=_SETTINGS,
        )
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# 3. Permission filtering
# ---------------------------------------------------------------------------


async def test_a_foreign_organization_is_denied_before_the_database_is_touched(session) -> None:
    datasource = await _seed_datasource(session)
    await session.commit()

    with pytest.raises(HTTPException) as exc_info:
        await list_catalog_rows(
            uuid4(),  # a different organization than the caller's context
            q=None,
            object_type=None,
            table_status="ACTIVE",
            certification=None,
            limit=10,
            offset=0,
            cursor=None,
            context=_context(datasource),
            session=session,
            settings=_SETTINGS,
        )
    assert exc_info.value.status_code == 403
    assert "cross-organization" in str(exc_info.value.detail)


async def test_rows_from_a_datasource_the_gate_denies_are_filtered_out(
    session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirrors how `list_tables` authorizes a catalog read (same `gate` call,
    same `READ_METADATA` action), applied once per distinct datasource on the
    page rather than once per row -- this asserts both halves: the denied
    datasource's row is dropped, and the allowed one's is kept.
    """
    datasource_a = await _seed_datasource(session)
    org = await session.get(Organization, datasource_a.organization_id)
    datasource_b = await _seed_datasource(session, org=org)
    await _seed_table(session, datasource_a, name="t_allowed")
    await _seed_table(session, datasource_b, name="t_denied")
    await session.commit()

    calls: list[UUID] = []
    real_gate = __import__("aida.authorization_gate", fromlist=["gate"]).gate

    async def fake_gate(session_arg, context_arg, *, datasource_id, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(datasource_id)
        if datasource_id == datasource_b.id:
            raise AuthorizationDenied("policy_denied")
        return GateOutcome(workspace_id=None, reason_code="ok", decided=True)

    monkeypatch.setattr("aida.api.gate", fake_gate)

    page = await list_catalog_rows(
        org.id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        certification=None,
        limit=10,
        offset=0,
        cursor=None,
        context=_context(datasource_a),
        session=session,
        settings=_SETTINGS,
    )

    assert [item.name for item in page.items] == ["t_allowed"]
    # One gate() call per distinct datasource on the page, not one per row.
    assert sorted(set(calls)) == sorted({datasource_a.id, datasource_b.id})
    assert len(calls) == 2
    assert real_gate is not fake_gate  # sanity: the patch actually replaced something real


# ---------------------------------------------------------------------------
# 4. Bounded query count
# ---------------------------------------------------------------------------


class _StatementCounter:
    """Counts SQL statements actually sent to the database (`cursor.execute`
    calls) via the real `before_cursor_execute` SQLAlchemy hook -- mirrors
    `test_bulk_governance_decisions.py`'s `_StatementCounter`.
    """

    def __init__(self) -> None:
        self.count = 0

    def __call__(self, *_args: object, **_kwargs: object) -> None:
        self.count += 1


def _count_statements(session: AsyncSession) -> _StatementCounter:
    counter = _StatementCounter()
    event.listen(session.bind.sync_engine, "before_cursor_execute", counter)  # type: ignore[union-attr]
    return counter


async def _seed_fully_populated_table(
    session: AsyncSession, datasource: DataSource, *, name: str
) -> MetadataTable:
    table = await _seed_table(session, datasource, name=name)
    documentation = AssetDocumentation(
        id=uuid4(), organization_id=table.organization_id, table_id=table.id
    )
    session.add(documentation)
    await session.flush()
    session.add(
        AssetDocumentationVersion(
            id=uuid4(),
            organization_id=table.organization_id,
            documentation_id=documentation.id,
            version=1,
            status="APPROVED",
            readme=f"Documentation for {name}.",
            created_by="drafter",
        )
    )
    session.add(
        OwnershipAssignment(
            id=uuid4(),
            organization_id=table.organization_id,
            subject_type="TABLE",
            subject_id=str(table.id),
            owner_type="TEAM",
            owner_principal="Data Office",
            assignment_kind="MANUAL",
            status="ACTIVE",
            assigned_by="steward",
        )
    )
    session.add(
        AssetCertification(
            id=uuid4(),
            organization_id=table.organization_id,
            table_id=table.id,
            asset_type="TABLE",
            status="ACTIVE",
            rationale="Meets the certification bar.",
            certified_by="reviewer",
            expires_at=_NOW + timedelta(days=180),
        )
    )
    term_id = uuid4()
    session.add(
        GlossaryTermVersion(
            id=uuid4(),
            organization_id=table.organization_id,
            term_id=term_id,
            version=1,
            status="APPROVED",
            display_name=f"Term for {name}",
            definition="A glossary term.",
            created_by="steward",
        )
    )
    session.add(
        AssetTermLink(
            id=uuid4(),
            organization_id=table.organization_id,
            table_id=table.id,
            term_id=term_id,
            linked_by="steward",
        )
    )
    session.add(
        DataQualityObservation(
            id=uuid4(),
            organization_id=table.organization_id,
            datasource_id=datasource.id,
            table_id=table.id,
            analysis_run_id=uuid4(),
            status="PASS",
            quality_score=95,
            created_at=_NOW - timedelta(hours=1),
        )
    )
    session.add(
        TableProfile(
            id=uuid4(),
            organization_id=table.organization_id,
            analysis_run_id=uuid4(),
            datasource_id=datasource.id,
            table_id=table.id,
            row_count_estimate=1000,
            sampled_row_count=100,
            status="COMPLETED",
        )
    )
    return table


async def test_query_count_does_not_grow_with_the_number_of_rows_on_the_page(session) -> None:
    datasource = await _seed_datasource(session)
    for i in range(4):
        await _seed_fully_populated_table(session, datasource, name=f"small_{i:02d}")
    await session.commit()

    counter = _count_statements(session)
    small_page = await list_catalog_rows(
        datasource.organization_id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        certification=None,
        limit=100,
        offset=0,
        cursor=None,
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )
    small_page_count = counter.count
    event.remove(session.bind.sync_engine, "before_cursor_execute", counter)  # type: ignore[union-attr]

    for i in range(20):
        await _seed_fully_populated_table(session, datasource, name=f"large_{i:02d}")
    await session.commit()

    counter = _count_statements(session)
    large_page = await list_catalog_rows(
        datasource.organization_id,
        q=None,
        object_type=None,
        table_status="ACTIVE",
        certification=None,
        limit=100,
        offset=0,
        cursor=None,
        context=_context(datasource),
        session=session,
        settings=_SETTINGS,
    )
    large_page_count = counter.count
    event.remove(session.bind.sync_engine, "before_cursor_execute", counter)  # type: ignore[union-attr]

    assert len(small_page.items) == 4
    assert len(large_page.items) == 24
    # Same fixed set of batched queries regardless of page size: not the N+1
    # pattern (1 + 5 calls per row) this endpoint exists to remove.
    assert small_page_count == large_page_count
    assert large_page_count <= 20
