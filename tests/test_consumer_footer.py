"""UX-18: the consumer footer on semantic-object edit/authoring surfaces.

`Docs/60-delivery/03-tracker.md` UX-18's exit criterion is that editing a
semantic object shows what consumes it *and at which version*, from CX-4
consumption lineage (`consumption_lineage.py`) -- "no semantic edit is made
blind". Two halves, mirroring `test_semantic_diff_endpoint.py`'s own split
between a composition module and the routes that wire it in:

1. ``test_compose_*`` -- unit tests directly against
   ``aida.consumer_footer.compose_consumer_footer``, the pure aggregation
   over ``ConsumptionRecord`` rows. The row's whole point is version
   specificity, so ``test_does_not_leak_consumers_of_a_different_version``
   seeds consumption records against two different version rows of the same
   kind of object and proves the footer composed for one version never
   reports the other version's consumers -- directly, not incidentally.
2. ``test_*_consumers_endpoint_*`` -- integration tests against the real,
   wired-in endpoints (``aida.semantic_api.get_semantic_metric_version_consumers``,
   ``get_semantic_model_version_consumers``, and
   ``aida.glossary_api.get_glossary_term_version_consumers``), calling the
   route functions directly against a real in-memory sqlite session, the
   same pattern ``test_semantic_diff_endpoint.py`` uses for
   ``get_governance_review_diff``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.consumer_footer import compose_consumer_footer
from aida.db import Base
from aida.glossary_api import get_glossary_term_version_consumers
from aida.main import app
from aida.models import (
    ConsumptionRecord,
    DataDomain,
    DataSource,
    GlossaryTerm,
    GlossaryTermVersion,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
)
from aida.security_types import SecurityContext
from aida.semantic_api import (
    get_semantic_metric_version_consumers,
    get_semantic_model_version_consumers,
)

_NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _context(org_id: UUID, *, roles: frozenset[str] | None = None) -> SecurityContext:
    return SecurityContext(
        principal_id="steward-1",
        principal_type="USER",
        organization_id=org_id,
        roles=roles or frozenset({"DataSteward"}),
    )


def _consumption(
    *,
    organization_id: UUID,
    resource_type: str,
    resource_id: str,
    consumer_id: str,
    consumer_type: str = "AGENT",
    channel: str = "MCP",
    consumed_at: datetime,
) -> ConsumptionRecord:
    return ConsumptionRecord(
        id=uuid4(),
        organization_id=organization_id,
        consumer_id=consumer_id,
        consumer_type=consumer_type,
        resource_type=resource_type,
        resource_id=resource_id,
        channel=channel,
        correlation_id=f"corr-{uuid4().hex[:8]}",
        policy_decision="ALLOW",
        consumed_at=consumed_at,
    )


class _Scenario:
    """Minimal org/project/table skeleton a `SemanticMetricVersion` needs for
    its `source_table_id` FK -- trimmed from `test_semantic_diff_endpoint.py`'s
    own `_Scenario` (no measure column, which is optional here).
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build(self) -> _Scenario:
        db = self.db
        self.organization = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
        db.add(self.organization)
        await db.flush()

        self.lob = LineOfBusiness(
            organization_id=self.organization.id, name="Retail", code="RETAIL"
        )
        db.add(self.lob)
        await db.flush()

        self.domain = DataDomain(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            name="Finance",
            code="FINANCE",
        )
        db.add(self.domain)
        await db.flush()

        self.project = Project(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            name="Core Banking",
            slug="core-banking",
        )
        db.add(self.project)
        await db.flush()

        self.datasource = DataSource(
            organization_id=self.organization.id,
            line_of_business_id=self.lob.id,
            data_domain_id=self.domain.id,
            project_id=self.project.id,
            name="core-warehouse",
            connector_type="POSTGRES",
            dialect="postgres",
            environment="PRODUCTION",
            credential_reference="vault://core-warehouse",
        )
        db.add(self.datasource)
        await db.flush()

        catalog = MetadataCatalog(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            name="bank",
            fingerprint="fp-catalog",
        )
        db.add(catalog)
        await db.flush()

        schema = MetadataSchema(
            organization_id=self.organization.id,
            catalog_id=catalog.id,
            name="finance",
            fingerprint="fp-schema",
        )
        db.add(schema)
        await db.flush()

        self.table = MetadataTable(
            organization_id=self.organization.id,
            datasource_id=self.datasource.id,
            schema_id=schema.id,
            name="fact_sales",
            object_type="TABLE",
            fingerprint="fp-table",
        )
        db.add(self.table)
        await db.flush()
        return self

    async def metric_version(
        self, *, version: int, status: str = "PUBLISHED"
    ) -> SemanticMetricVersion:
        db = self.db
        model = SemanticModelVersion(
            organization_id=self.organization.id,
            project_id=self.project.id,
            version=version,
            name="Sales Model",
            change_summary="metric maintenance",
            status=status,
            created_by="metric-maker",
        )
        db.add(model)
        await db.flush()
        metric = SemanticMetric(
            organization_id=self.organization.id,
            project_id=self.project.id,
            slug=f"revenue-{uuid4().hex[:6]}",
        )
        db.add(metric)
        await db.flush()
        metric_version = SemanticMetricVersion(
            organization_id=self.organization.id,
            semantic_model_version_id=model.id,
            metric_id=metric.id,
            version=version,
            status=status,
            name="Revenue",
            description="Total revenue",
            aggregation="SUM",
            grain="daily",
            source_table_id=self.table.id,
            fingerprint=f"fp-revenue-{version}",
            created_by="metric-maker",
        )
        db.add(metric_version)
        await db.flush()
        return metric_version


# ---------------------------------------------------------------------------
# Pure composition: aida.consumer_footer.compose_consumer_footer
# ---------------------------------------------------------------------------


async def test_compose_aggregates_events_per_distinct_consumer(session: AsyncSession) -> None:
    org_id = uuid4()
    session.add(Organization(id=org_id, name="Bank", slug=f"bank-{uuid4().hex[:8]}"))
    await session.flush()
    resource_id = str(uuid4())
    session.add_all(
        [
            _consumption(
                organization_id=org_id,
                resource_type="semantic_metric_version",
                resource_id=resource_id,
                consumer_id="agent-1",
                consumed_at=_NOW - timedelta(hours=2),
            ),
            _consumption(
                organization_id=org_id,
                resource_type="semantic_metric_version",
                resource_id=resource_id,
                consumer_id="agent-1",
                consumed_at=_NOW,
            ),
            _consumption(
                organization_id=org_id,
                resource_type="semantic_metric_version",
                resource_id=resource_id,
                consumer_id="user-2",
                consumer_type="USER",
                channel="REST",
                consumed_at=_NOW - timedelta(hours=1),
            ),
        ]
    )
    await session.flush()

    footer = await compose_consumer_footer(
        session,
        organization_id=org_id,
        resource_type="semantic_metric_version",
        resource_id=resource_id,
        version=3,
        now=_NOW,
    )

    assert footer.resource_type == "semantic_metric_version"
    assert footer.resource_id == resource_id
    assert footer.version == 3
    assert footer.total_consumption_events == 3
    assert footer.total_consumers == 2
    by_id = {entry.consumer_id: entry for entry in footer.consumers}
    assert by_id["agent-1"].consumption_count == 2
    # sqlite drops tzinfo on round-trip; compare on wall-clock value only.
    assert by_id["agent-1"].last_consumed_at.replace(tzinfo=UTC) == _NOW
    assert by_id["user-2"].consumption_count == 1
    assert by_id["user-2"].channel == "REST"
    # newest-consumer-first
    assert footer.consumers[0].consumer_id == "agent-1"


async def test_compose_returns_empty_footer_when_never_consumed(session: AsyncSession) -> None:
    org_id = uuid4()
    session.add(Organization(id=org_id, name="Bank", slug=f"bank-{uuid4().hex[:8]}"))
    await session.flush()

    footer = await compose_consumer_footer(
        session,
        organization_id=org_id,
        resource_type="glossary_term_version",
        resource_id=str(uuid4()),
        version=1,
        now=_NOW,
    )

    assert footer.consumers == []
    assert footer.total_consumers == 0
    assert footer.total_consumption_events == 0


async def test_does_not_leak_consumers_of_a_different_version(session: AsyncSession) -> None:
    """The row's central claim: `resource_id` for a versioned object is that
    *version row's own primary key*, so a consumption edge recorded against
    one version can never surface as a consumer of a sibling version of the
    same logical object.
    """
    org_id = uuid4()
    session.add(Organization(id=org_id, name="Bank", slug=f"bank-{uuid4().hex[:8]}"))
    await session.flush()
    version_3_id = str(uuid4())
    version_4_id = str(uuid4())
    session.add_all(
        [
            _consumption(
                organization_id=org_id,
                resource_type="semantic_metric_version",
                resource_id=version_3_id,
                consumer_id="agent-on-v3",
                consumed_at=_NOW - timedelta(hours=1),
            ),
            _consumption(
                organization_id=org_id,
                resource_type="semantic_metric_version",
                resource_id=version_4_id,
                consumer_id="agent-on-v4",
                consumed_at=_NOW,
            ),
        ]
    )
    await session.flush()

    footer_v3 = await compose_consumer_footer(
        session,
        organization_id=org_id,
        resource_type="semantic_metric_version",
        resource_id=version_3_id,
        version=3,
        now=_NOW,
    )
    footer_v4 = await compose_consumer_footer(
        session,
        organization_id=org_id,
        resource_type="semantic_metric_version",
        resource_id=version_4_id,
        version=4,
        now=_NOW,
    )

    assert [entry.consumer_id for entry in footer_v3.consumers] == ["agent-on-v3"]
    assert [entry.consumer_id for entry in footer_v4.consumers] == ["agent-on-v4"]
    # explicit negative check: neither version's consumer appears in the
    # other version's footer
    assert "agent-on-v4" not in [entry.consumer_id for entry in footer_v3.consumers]
    assert "agent-on-v3" not in [entry.consumer_id for entry in footer_v4.consumers]


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def test_consumer_footer_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/semantic-metric-versions/{version_id}/consumers" in paths
    assert "/v1/semantic-model-versions/{model_id}/consumers" in paths
    assert "/v1/glossary-term-versions/{version_id}/consumers" in paths


# ---------------------------------------------------------------------------
# Integration: semantic metric version consumers endpoint
# ---------------------------------------------------------------------------


async def test_metric_version_consumers_endpoint_is_version_specific(
    session: AsyncSession,
) -> None:
    scenario = await _Scenario(session).build()
    v1 = await scenario.metric_version(version=1, status="SUPERSEDED")
    v2 = await scenario.metric_version(version=2, status="PUBLISHED")
    session.add(
        _consumption(
            organization_id=scenario.organization.id,
            resource_type="semantic_metric_version",
            resource_id=str(v1.id),
            consumer_id="reporting-agent",
            consumed_at=_NOW,
        )
    )
    await session.flush()

    footer_v1 = await get_semantic_metric_version_consumers(
        v1.id, context=_context(scenario.organization.id), session=session
    )
    footer_v2 = await get_semantic_metric_version_consumers(
        v2.id, context=_context(scenario.organization.id), session=session
    )

    assert footer_v1.version == 1
    assert [entry.consumer_id for entry in footer_v1.consumers] == ["reporting-agent"]
    assert footer_v2.version == 2
    assert footer_v2.consumers == []


async def test_metric_version_consumers_missing_version_is_404(session: AsyncSession) -> None:
    org_id = uuid4()
    session.add(Organization(id=org_id, name="Bank", slug=f"bank-{uuid4().hex[:8]}"))
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await get_semantic_metric_version_consumers(
            uuid4(), context=_context(org_id), session=session
        )
    assert exc_info.value.status_code == 404


async def test_metric_version_consumers_enforces_organization_boundary(
    session: AsyncSession,
) -> None:
    scenario = await _Scenario(session).build()
    v1 = await scenario.metric_version(version=1)

    with pytest.raises(HTTPException) as exc_info:
        await get_semantic_metric_version_consumers(
            v1.id, context=_context(uuid4()), session=session
        )
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# Integration: semantic model version consumers endpoint
# ---------------------------------------------------------------------------


async def test_model_version_consumers_endpoint_is_version_specific(
    session: AsyncSession,
) -> None:
    scenario = await _Scenario(session).build()
    model_v1 = SemanticModelVersion(
        organization_id=scenario.organization.id,
        project_id=scenario.project.id,
        version=1,
        name="Sales Model",
        change_summary="initial",
        status="SUPERSEDED",
        created_by="model-maker",
    )
    model_v2 = SemanticModelVersion(
        organization_id=scenario.organization.id,
        project_id=scenario.project.id,
        version=2,
        name="Sales Model",
        change_summary="revision",
        status="PUBLISHED",
        created_by="model-maker",
    )
    session.add_all([model_v1, model_v2])
    await session.flush()
    session.add(
        _consumption(
            organization_id=scenario.organization.id,
            resource_type="semantic_model_version",
            resource_id=str(model_v2.id),
            consumer_id="context-product-compiler",
            consumer_type="CONTEXT_PRODUCT",
            channel="INTERNAL",
            consumed_at=_NOW,
        )
    )
    await session.flush()

    footer_v1 = await get_semantic_model_version_consumers(
        model_v1.id, context=_context(scenario.organization.id), session=session
    )
    footer_v2 = await get_semantic_model_version_consumers(
        model_v2.id, context=_context(scenario.organization.id), session=session
    )

    assert footer_v1.consumers == []
    assert [entry.consumer_id for entry in footer_v2.consumers] == ["context-product-compiler"]
    assert footer_v2.version == 2


# ---------------------------------------------------------------------------
# Integration: glossary term version consumers endpoint
# ---------------------------------------------------------------------------


async def test_glossary_term_version_consumers_endpoint_is_version_specific(
    session: AsyncSession,
) -> None:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    term = GlossaryTerm(organization_id=org.id, term_key="net-sales")
    session.add(term)
    await session.flush()
    published = GlossaryTermVersion(
        organization_id=org.id,
        term_id=term.id,
        version=1,
        status="APPROVED",
        display_name="Net Sales",
        definition="Total sale value after adjustments.",
        synonyms=["revenue"],
        created_by="term-maker",
        approved_by="term-checker",
        approved_at=_NOW,
    )
    draft = GlossaryTermVersion(
        organization_id=org.id,
        term_id=term.id,
        version=2,
        status="DRAFT",
        display_name="Net Sales",
        definition="Total sale value after adjustments and returns.",
        synonyms=["revenue", "net revenue"],
        created_by="term-maker",
    )
    session.add_all([published, draft])
    await session.flush()
    session.add(
        _consumption(
            organization_id=org.id,
            resource_type="glossary_term_version",
            resource_id=str(published.id),
            consumer_id="bi-dashboard",
            consumer_type="CONTEXT_PRODUCT",
            channel="MCP",
            consumed_at=_NOW,
        )
    )
    await session.flush()

    footer_published = await get_glossary_term_version_consumers(
        published.id, context=_context(org.id), session=session
    )
    footer_draft = await get_glossary_term_version_consumers(
        draft.id, context=_context(org.id), session=session
    )

    assert footer_published.version == 1
    assert [entry.consumer_id for entry in footer_published.consumers] == ["bi-dashboard"]
    # The steward is about to edit the still-unconsumed draft (version 2) --
    # its footer must not inherit version 1's consumers. This is the row's
    # own framing: an edit to a *never-consumed* draft is honestly blind to
    # nothing, whereas editing the published version 1 (were that the flow)
    # would show the real downstream impact above.
    assert footer_draft.version == 2
    assert footer_draft.consumers == []


async def test_glossary_term_version_consumers_missing_version_is_404(
    session: AsyncSession,
) -> None:
    org_id = uuid4()
    session.add(Organization(id=org_id, name="Bank", slug=f"bank-{uuid4().hex[:8]}"))
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await get_glossary_term_version_consumers(
            uuid4(), context=_context(org_id), session=session
        )
    assert exc_info.value.status_code == 404


async def test_glossary_term_version_consumers_enforces_organization_boundary(
    session: AsyncSession,
) -> None:
    org = Organization(name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    term = GlossaryTerm(organization_id=org.id, term_key="net-sales")
    session.add(term)
    await session.flush()
    version = GlossaryTermVersion(
        organization_id=org.id,
        term_id=term.id,
        version=1,
        status="APPROVED",
        display_name="Net Sales",
        definition="Total sale value after adjustments.",
        synonyms=[],
        created_by="term-maker",
    )
    session.add(version)
    await session.flush()

    with pytest.raises(HTTPException) as exc_info:
        await get_glossary_term_version_consumers(
            version.id, context=_context(uuid4()), session=session
        )
    assert exc_info.value.status_code == 403
