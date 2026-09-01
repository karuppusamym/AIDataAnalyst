"""AT-17: metric-formula collision detection, wired into the endpoint.

Integration tests against a real (in-memory sqlite) database with rows
seeded directly through the ORM -- the same `_Scenario` pattern
`test_semantic_diff_endpoint.py` (SM-7) already established for this module,
reused here so the actual SQL join that assembles published metric-version
snapshots runs for real, not just the pure `aida.metric_formula_signature`
logic already covered in isolation by `tests/test_metric_formula_signature.py`.

Also proves the "reuse GL-3's infrastructure, don't invent a parallel
conflict table" claim: a detected collision is a real `GlossaryConflict` row
with `term_id=None`, and it resolves through the exact same
`stewardship_api.submit_conflict_resolution` / governance-review-decision
path GL-3 built for glossary terms.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from aida.db import Base
from aida.main import app
from aida.models import (
    DataDomain,
    DataSource,
    GlossaryConflict,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    SemanticMetric,
    SemanticMetricVersion,
    SemanticModelVersion,
)
from aida.schemas import GlossaryConflictResolution
from aida.security_types import SecurityContext
from aida.semantic_api import detect_metric_formula_collisions, list_metric_formula_collisions
from aida.stewardship_api import submit_conflict_resolution
from aida.stewardship_service import apply_conflict_resolution

# ---------------------------------------------------------------------------
# Contract: the routes are registered
# ---------------------------------------------------------------------------


def test_metric_conflict_routes_are_registered() -> None:
    paths = app.openapi()["paths"]
    assert "/v1/organizations/{organization_id}/metric-conflicts/detect" in paths
    assert "/v1/organizations/{organization_id}/metric-conflicts" in paths
    # AT-17 resolves through GL-3's own resolution route -- no parallel one added.
    assert "/v1/glossary-conflicts/{conflict_id}/resolution" in paths


# ---------------------------------------------------------------------------
# Fixtures / seeding helpers
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _context(
    org_id: UUID, *, principal: str = "steward", roles: frozenset[str] | None = None
) -> SecurityContext:
    return SecurityContext(
        principal_id=principal,
        principal_type="USER",
        organization_id=org_id,
        roles=roles or frozenset({"DataSteward"}),
    )


class _Scenario:
    """Minimal org/project/table skeleton a `SemanticMetricVersion` needs --
    mirrors `test_semantic_diff_endpoint.py`'s own `_Scenario.build`.
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self._next_model_version = 1

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

        self.measure_column = MetadataColumn(
            organization_id=self.organization.id,
            table_id=self.table.id,
            name="sale_amount",
            ordinal_position=1,
            physical_type="NUMERIC",
            nullable=False,
            fingerprint="fp-column",
        )
        db.add(self.measure_column)
        await db.flush()
        return self

    async def published_metric(
        self,
        *,
        slug: str,
        name: str,
        aggregation: str,
        grain: str,
        measure_column_id: UUID | None,
    ) -> SemanticMetricVersion:
        db = self.db
        model_version = self._next_model_version
        self._next_model_version += 1
        model = SemanticModelVersion(
            organization_id=self.organization.id,
            project_id=self.project.id,
            version=model_version,
            name=f"{slug} model",
            change_summary="initial",
            status="PUBLISHED",
            created_by="metric-maker",
        )
        db.add(model)
        await db.flush()
        metric = SemanticMetric(
            organization_id=self.organization.id, project_id=self.project.id, slug=slug
        )
        db.add(metric)
        await db.flush()
        metric_version = SemanticMetricVersion(
            organization_id=self.organization.id,
            semantic_model_version_id=model.id,
            metric_id=metric.id,
            version=1,
            status="PUBLISHED",
            name=name,
            description=f"{name} description",
            aggregation=aggregation,
            grain=grain,
            source_table_id=self.table.id,
            measure_column_id=measure_column_id,
            fingerprint=f"fp-{slug}",
            created_by="metric-maker",
        )
        db.add(metric_version)
        await db.flush()
        return metric_version


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


async def test_detect_flags_two_differently_named_metrics_with_identical_formula(
    session: AsyncSession,
) -> None:
    scenario = await _Scenario(session).build()
    await scenario.published_metric(
        slug="net-revenue",
        name="Net Revenue",
        aggregation="SUM",
        grain="daily",
        measure_column_id=scenario.measure_column.id,
    )
    await scenario.published_metric(
        slug="daily-sales-total",
        name="Daily Sales Total",
        aggregation="SUM",
        grain="daily",
        measure_column_id=scenario.measure_column.id,
    )

    page = await detect_metric_formula_collisions(
        scenario.organization.id, _context(scenario.organization.id), session
    )

    assert page.total == 1
    conflict = page.items[0]
    assert conflict.conflict_type == "METRIC_FORMULA_COLLISION"
    assert conflict.term_id is None
    assert conflict.status == "OPEN"
    assert {conflict.position_a["metric_name"], conflict.position_b["metric_name"]} == {
        "Net Revenue",
        "Daily Sales Total",
    }
    assert conflict.position_a["match_kind"] == "EXACT_MATCH"

    # Persisted as a real GlossaryConflict row (reused infra, not a parallel table).
    stored = (await session.scalars(select(GlossaryConflict))).all()
    assert len(stored) == 1
    assert stored[0].term_id is None
    assert stored[0].conflict_type == "METRIC_FORMULA_COLLISION"


async def test_detect_flags_grain_label_collision_as_normalized_match(
    session: AsyncSession,
) -> None:
    scenario = await _Scenario(session).build()
    await scenario.published_metric(
        slug="net-revenue",
        name="Net Revenue",
        aggregation="SUM",
        grain="Daily",
        measure_column_id=scenario.measure_column.id,
    )
    await scenario.published_metric(
        slug="daily-sales-total",
        name="Daily Sales Total",
        aggregation="SUM",
        grain=" daily ",
        measure_column_id=scenario.measure_column.id,
    )

    page = await detect_metric_formula_collisions(
        scenario.organization.id, _context(scenario.organization.id), session
    )

    assert page.total == 1
    assert page.items[0].position_a["match_kind"] == "NORMALIZED_GRAIN_MATCH"


async def test_detect_does_not_flag_genuinely_different_metrics(session: AsyncSession) -> None:
    scenario = await _Scenario(session).build()
    await scenario.published_metric(
        slug="net-revenue",
        name="Net Revenue",
        aggregation="SUM",
        grain="daily",
        measure_column_id=scenario.measure_column.id,
    )
    await scenario.published_metric(
        slug="order-count",
        name="Order Count",
        aggregation="COUNT",
        grain="daily",
        measure_column_id=None,
    )

    page = await detect_metric_formula_collisions(
        scenario.organization.id, _context(scenario.organization.id), session
    )

    assert page.total == 0
    assert (await session.scalars(select(GlossaryConflict))).all() == []


async def test_detect_is_idempotent_against_an_already_open_conflict(
    session: AsyncSession,
) -> None:
    scenario = await _Scenario(session).build()
    await scenario.published_metric(
        slug="net-revenue",
        name="Net Revenue",
        aggregation="SUM",
        grain="daily",
        measure_column_id=scenario.measure_column.id,
    )
    await scenario.published_metric(
        slug="daily-sales-total",
        name="Daily Sales Total",
        aggregation="SUM",
        grain="daily",
        measure_column_id=scenario.measure_column.id,
    )

    first = await detect_metric_formula_collisions(
        scenario.organization.id, _context(scenario.organization.id), session
    )
    second = await detect_metric_formula_collisions(
        scenario.organization.id, _context(scenario.organization.id), session
    )

    assert first.total == 1
    assert second.total == 0
    assert len((await session.scalars(select(GlossaryConflict))).all()) == 1


# ---------------------------------------------------------------------------
# Read side
# ---------------------------------------------------------------------------


async def test_list_metric_conflicts_only_returns_metric_formula_collisions(
    session: AsyncSession,
) -> None:
    scenario = await _Scenario(session).build()
    await scenario.published_metric(
        slug="net-revenue",
        name="Net Revenue",
        aggregation="SUM",
        grain="daily",
        measure_column_id=scenario.measure_column.id,
    )
    await scenario.published_metric(
        slug="daily-sales-total",
        name="Daily Sales Total",
        aggregation="SUM",
        grain="daily",
        measure_column_id=scenario.measure_column.id,
    )
    await detect_metric_formula_collisions(
        scenario.organization.id, _context(scenario.organization.id), session
    )
    # A GL-3-style glossary synonym conflict in the same org must not leak in.
    session.add(
        GlossaryConflict(
            organization_id=scenario.organization.id,
            term_id=None,
            conflict_type="SYNONYM_COLLISION",
            position_a={"term_id": str(uuid4())},
            position_b={"term_id": str(uuid4())},
            raised_by="steward",
        )
    )
    await session.commit()

    page = await list_metric_formula_collisions(
        scenario.organization.id,
        None,
        100,
        0,
        _context(scenario.organization.id, roles=frozenset({"Viewer"})),
        session,
    )

    assert page.total == 1
    assert page.items[0].conflict_type == "METRIC_FORMULA_COLLISION"


# ---------------------------------------------------------------------------
# Resolution reuses GL-3's maker-checker path unmodified
# ---------------------------------------------------------------------------


async def test_metric_conflict_resolves_through_gl3s_existing_resolution_path(
    session: AsyncSession,
) -> None:
    scenario = await _Scenario(session).build()
    await scenario.published_metric(
        slug="net-revenue",
        name="Net Revenue",
        aggregation="SUM",
        grain="daily",
        measure_column_id=scenario.measure_column.id,
    )
    await scenario.published_metric(
        slug="daily-sales-total",
        name="Daily Sales Total",
        aggregation="SUM",
        grain="daily",
        measure_column_id=scenario.measure_column.id,
    )
    detected = await detect_metric_formula_collisions(
        scenario.organization.id, _context(scenario.organization.id), session
    )
    conflict_id = detected.items[0].id
    original_position_a = dict(detected.items[0].position_a)
    original_position_b = dict(detected.items[0].position_b)

    review = await submit_conflict_resolution(
        conflict_id,
        GlossaryConflictResolution(
            resolution="ACCEPT_POSITION_A",
            rationale="Net Revenue is the canonical metric; deprecating the duplicate.",
        ),
        _context(scenario.organization.id),
        session,
    )
    assert review.status == "PENDING"

    conflict = await session.get(GlossaryConflict, conflict_id)
    assert conflict is not None
    assert conflict.status == "REVIEW_REQUIRED"
    # Losing position retained through the resolution proposal, same as GL-3.
    assert conflict.position_a == original_position_a
    assert conflict.position_b == original_position_b

    event_type = await apply_conflict_resolution(
        conflict, reviewer="checker", now=conflict.created_at
    )
    assert event_type == "glossary.conflict_resolved.v1"
    assert conflict.status == "RESOLVED"
    # Both positions are still on the record after resolution -- nothing deleted.
    assert conflict.position_a == original_position_a
    assert conflict.position_b == original_position_b
