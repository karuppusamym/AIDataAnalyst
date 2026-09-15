"""R11-FP12: a context product can name routines, and says what its routines and views stand on.

Before this a product was scoped by table ids alone: it could hand an agent "these tables" but not
"this procedure builds that one", nor "this view's definition was withheld from us". The tests pin
four rules:

* a product that names no routine and holds no view compiles, and fingerprints, byte-identically
  to before -- no deployed artifact or etag goes stale because the field exists;
* routine references reach every target that embeds the common context, while the coverage
  section reaches only the Atlas-native targets, like negative knowledge and exemplars;
* coverage never mentions a table outside the product -- not by id, not by count -- and keeps an
  undecided lineage proposal, a withheld definition and a captured one apart;
* a version may name only ACTIVE routines in its own project.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import yaml
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.context_compiler import (
    ResolvedRoutineReference,
    ResolvedTableReference,
    compile_context_product,
)
from aida.context_product_api import (
    context_product_fingerprint,
    list_context_product_routine_options,
    validate_context_product_references,
)
from aida.context_product_coverage import load_routine_references, load_view_coverage
from aida.db import Base
from aida.envelope_models import MetadataRoutine, MetadataViewDefinition
from aida.models import (
    ContextProduct,
    ContextProductVersion,
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
    ViewLineageEdge,
)
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.schemas import ContextProductDefinition
from aida.security import SecurityContext

BODY = "CREATE PROCEDURE rebuild() LANGUAGE plpgsql AS $$ BEGIN NULL; END; $$"
VIEW_SQL = "SELECT id FROM sales.orders"


# --- the compiled artifact ------------------------------------------------------------------


def _definition(**changes: object) -> ContextProductDefinition:
    values: dict[str, object] = {
        "name": "Revenue context",
        "description": "Approved revenue metadata.",
        "purpose": "Support bounded revenue analysis.",
        "owner_type": "GROUP",
        "owner_principal": "revenue-owner",
        "table_ids": [uuid4()],
        "allowed_consumer_roles": ["Analyst"],
    }
    values.update(changes)
    return ContextProductDefinition.model_validate(values)


def _fixture() -> tuple[ContextProduct, ContextProductVersion, list[ResolvedTableReference]]:
    now = datetime(2026, 9, 15, tzinfo=UTC)
    product = ContextProduct(
        id=uuid4(),
        organization_id=uuid4(),
        project_id=uuid4(),
        product_key="revenue_context",
        lifecycle_status="ACTIVE",
        created_by="maker",
        created_at=now,
        updated_at=now,
    )
    table_id = str(uuid4())
    version = ContextProductVersion(
        id=uuid4(),
        organization_id=product.organization_id,
        product_id=product.id,
        version=1,
        status="PUBLISHED",
        name="Revenue context",
        description="Approved revenue metadata.",
        purpose="Support bounded revenue analysis.",
        owner_principal="revenue-owner",
        table_ids=[table_id],
        semantic_model_version_ids=[],
        glossary_term_version_ids=[],
        eligible_tool_version_ids=[],
        allowed_consumer_roles=["Analyst"],
        lineage_depth=2,
        quality_requirements={"minimum_score": 0},
        policy_summary={"source_values": "GATEWAY_ONLY"},
        fingerprint="a" * 64,
        created_by="maker",
        created_at=now,
        updated_at=now,
    )
    return product, version, [ResolvedTableReference(table_id, "BANK.SALES.ORDERS")]


def _routine_reference(table_id: str) -> ResolvedRoutineReference:
    return ResolvedRoutineReference(
        routine_id=str(uuid4()),
        qualified_name="BANK.SALES.REBUILD",
        routine_type="PROCEDURE",
        signature="()",
        status="ACTIVE",
        definition_available=True,
        lineage="ACTIVE",
        fully_parsed=True,
        reads_table_ids=(table_id,),
        writes_table_ids=(),
        definition_digest="d" * 64,
    )


def test_a_product_with_no_routine_or_view_compiles_and_fingerprints_as_before() -> None:
    product, version, tables = _fixture()

    for target in ("MCP", "REST", "YAML", "OSI"):
        before = compile_context_product(product, version, target, tables)
        after = compile_context_product(product, version, target, tables, None, None, [], [])
        assert after.artifact_hash == before.artifact_hash
        assert "coverage" not in after.content
        assert "routines" not in after.content

    definition = _definition()
    legacy = definition.model_dump(mode="json")
    legacy.pop("routine_ids")
    expected = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert context_product_fingerprint(definition) == expected
    assert context_product_fingerprint(_definition(routine_ids=[uuid4()])) != expected


def test_routine_references_reach_common_targets_and_coverage_only_atlas_native() -> None:
    product, version, tables = _fixture()
    routine = _routine_reference(tables[0].table_id)

    for target, envelope in (("MCP", "context"), ("REST", "context"), ("YAML", "spec")):
        compiled = compile_context_product(
            product, version, target, tables, routines=[routine]
        )
        body = (yaml.safe_load if target == "YAML" else json.loads)(compiled.content)[envelope]
        assert body["references"]["routines"] == [
            {"id": routine.routine_id, "qualified_name": "BANK.SALES.REBUILD"}
        ]
        (covered,) = body["coverage"]["routines"]
        assert covered["reads_table_ids"] == [tables[0].table_id]
        assert covered["lineage"] == "ACTIVE"

    osi = json.loads(
        compile_context_product(product, version, "OSI", tables, routines=[routine]).content
    )
    assert osi["semanticContext"]["references"]["routines"][0]["id"] == routine.routine_id
    assert "coverage" not in osi["semanticContext"]
    for target in ("ODCS", "SNOWFLAKE_SEMANTIC_VIEW", "DATABRICKS_METRIC_VIEW"):
        content = compile_context_product(
            product, version, target, tables, routines=[routine]
        ).content
        assert "coverage" not in content


# --- resolution against the catalog ---------------------------------------------------------


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


async def _source(
    session: AsyncSession, org: Organization, lob: LineOfBusiness, domain: DataDomain, name: str
) -> tuple[Project, DataSource, MetadataSchema]:
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name=name,
        slug=f"{name}-{uuid4().hex[:8]}",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=name,
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
        status="ACTIVE",
    )
    session.add_all([project, datasource])
    await session.flush()
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="bank",
        fingerprint="f",
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="sales", fingerprint="f"
    )
    session.add(schema)
    await session.flush()
    return project, datasource, schema


async def _seed(session: AsyncSession) -> dict[str, object]:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    lob = LineOfBusiness(id=uuid4(), organization_id=org.id, name="R", code=f"R{uuid4().hex[:6]}")
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Finance",
        code=f"F{uuid4().hex[:6]}",
    )
    session.add_all([org, lob, domain])
    await session.flush()
    project, datasource, schema = await _source(session, org, lob, domain, "warehouse")
    _, elsewhere_source, elsewhere_schema = await _source(session, org, lob, domain, "elsewhere")

    tables: dict[str, MetadataTable] = {}
    for name, object_type in (
        ("orders", "BASE_TABLE"),
        ("totals", "BASE_TABLE"),
        ("ledger", "BASE_TABLE"),
        ("orders_v", "VIEW"),
        ("secret_v", "MATERIALIZED VIEW"),
    ):
        table = MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=name,
            object_type=object_type,
            status="ACTIVE",
            fingerprint="f",
        )
        session.add(table)
        tables[name] = table
    await session.flush()
    session.add_all(
        [
            MetadataViewDefinition(
                organization_id=org.id,
                datasource_id=datasource.id,
                table_id=tables["orders_v"].id,
                definition_sql_redacted=VIEW_SQL,
                redaction_status="PARSED",
                screening_status="CLEAN",
                availability="AVAILABLE",
                fingerprint="f",
            ),
            MetadataViewDefinition(
                organization_id=org.id,
                datasource_id=datasource.id,
                table_id=tables["secret_v"].id,
                definition_sql_redacted=None,
                availability="UNAVAILABLE",
                unavailable_reason="definition not visible to the scanning principal",
                fingerprint="f",
            ),
            ViewLineageEdge(
                organization_id=org.id,
                datasource_id=datasource.id,
                source_table="sales.orders",
                source_column="id",
                target_table="sales.orders_v",
                target_column="id",
                source_table_id=tables["orders"].id,
                target_table_id=tables["orders_v"].id,
                transformation_type="DIRECT",
                confidence="FULL",
                dialect="postgres",
                sql_hash="h",
                review_status="ACTIVE",
            ),
        ]
    )

    def routine(
        name: str, *, status: str = "ACTIVE", where: MetadataSchema = schema
    ) -> MetadataRoutine:
        row = MetadataRoutine(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=elsewhere_source.id if where is elsewhere_schema else datasource.id,
            schema_id=where.id,
            name=name,
            signature="()",
            routine_type="PROCEDURE",
            language="plpgsql",
            body_sql_redacted=BODY,
            redaction_status="LEXICAL",
            screening_status="CLEAN",
            status=status,
            fingerprint="f",
        )
        session.add(row)
        return row

    routines = {
        "rebuild": routine("rebuild"),
        "draft_only": routine("draft_only"),
        "gone": routine("gone", status="DEPRECATED"),
        "elsewhere": routine("rebuild", where=elsewhere_schema),
    }
    await session.flush()

    def edge(
        owner: str, source: str, target: str, *, review_status: str = "ACTIVE", ordinal: int = 1
    ) -> DeepProcedureLineageEdge:
        return DeepProcedureLineageEdge(
            organization_id=org.id,
            datasource_id=datasource.id,
            routine_id=routines[owner].id,
            statement_ordinal=ordinal,
            source_table=f"sales.{source}",
            source_column="amount",
            target_table=f"sales.{target}",
            target_column="amount",
            source_table_id=tables[source].id,
            target_table_id=tables[target].id,
            transformation_type="DIRECT",
            confidence="FULL",
            dialect="postgres",
            is_write=True,
            sql_hash="h",
            review_status=review_status,
        )

    session.add_all(
        [
            edge("rebuild", "orders", "totals"),
            # Reads a table the product does not cover: the coverage must not say so.
            edge("rebuild", "ledger", "totals", ordinal=2),
            edge("draft_only", "orders", "totals", review_status="PROPOSED"),
        ]
    )
    await session.commit()
    return {"org": org, "project": project, "tables": tables, "routines": routines}


def _ids(seeded: dict[str, object], group: str, *names: str) -> list[str]:
    rows = seeded[group]
    assert isinstance(rows, dict)
    return [str(rows[name].id) for name in names]


@pytest.mark.asyncio
async def test_routine_coverage_reports_lineage_state_and_no_table_outside_the_product(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)
    org = seeded["org"]
    assert isinstance(org, Organization)
    scope = _ids(seeded, "tables", "orders", "totals", "orders_v", "secret_v")

    resolved = await load_routine_references(
        session, org.id, _ids(seeded, "routines", "rebuild", "draft_only"), scope
    )

    by_name = {item.qualified_name: item for item in resolved}
    rebuild = by_name["bank.sales.rebuild"]
    assert rebuild.lineage == "ACTIVE" and rebuild.fully_parsed
    assert list(rebuild.reads_table_ids) == _ids(seeded, "tables", "orders")
    assert list(rebuild.writes_table_ids) == _ids(seeded, "tables", "totals")
    assert rebuild.definition_available
    assert rebuild.definition_digest == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    assert _ids(seeded, "tables", "ledger")[0] not in repr(resolved)

    draft_only = by_name["bank.sales.draft_only"]
    assert draft_only.lineage == "PROPOSED" and not draft_only.fully_parsed
    assert draft_only.reads_table_ids == () and draft_only.writes_table_ids == ()

    other_organization = await load_routine_references(
        session, uuid4(), _ids(seeded, "routines", "rebuild"), scope
    )
    assert other_organization == []


@pytest.mark.asyncio
async def test_view_coverage_keeps_a_withheld_definition_apart_from_a_captured_one(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)
    org = seeded["org"]
    assert isinstance(org, Organization)

    coverage = await load_view_coverage(
        session, org.id, _ids(seeded, "tables", "orders", "orders_v", "secret_v")
    )

    by_id = {item.table_id: item for item in coverage}
    (captured_id,) = _ids(seeded, "tables", "orders_v")
    (withheld_id,) = _ids(seeded, "tables", "secret_v")
    assert set(by_id) == {captured_id, withheld_id}, "a plain table is not a view"
    captured = by_id[captured_id]
    assert (captured.object_type, captured.definition_available, captured.lineage) == (
        "VIEW",
        True,
        "ACTIVE",
    )
    assert captured.definition_digest == hashlib.sha256(VIEW_SQL.encode("utf-8")).hexdigest()
    withheld = by_id[withheld_id]
    assert (withheld.object_type, withheld.definition_available, withheld.lineage) == (
        "MATERIALIZED_VIEW",
        False,
        "NONE",
    )
    assert withheld.definition_digest is None


@pytest.mark.asyncio
async def test_the_picker_offers_exactly_the_routines_a_draft_may_name(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)
    project = seeded["project"]
    org = seeded["org"]
    assert isinstance(project, Project) and isinstance(org, Organization)
    steward = SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=org.id,
        roles=frozenset({"DataSteward"}),
    )

    options = await list_context_product_routine_options(project.id, 200, steward, session)

    # Not the DEPRECATED routine, not the other project's routine of the same name.
    assert sorted(option.name for option in options) == ["draft_only", "rebuild"]
    assert {str(option.id) for option in options} == set(
        _ids(seeded, "routines", "rebuild", "draft_only")
    )
    outsider = SecurityContext(
        principal_id="steward@example.com",
        principal_type="USER",
        organization_id=uuid4(),
        roles=frozenset({"DataSteward"}),
    )
    with pytest.raises(HTTPException):
        await list_context_product_routine_options(project.id, 200, outsider, session)


@pytest.mark.asyncio
async def test_a_version_may_name_only_active_routines_in_its_own_project(
    session: AsyncSession,
) -> None:
    seeded = await _seed(session)
    project = seeded["project"]
    assert isinstance(project, Project)
    orders = _ids(seeded, "tables", "orders")

    await validate_context_product_references(
        session,
        project,
        _definition(table_ids=orders, routine_ids=_ids(seeded, "routines", "rebuild")),
    )
    for refused in ("gone", "elsewhere"):
        with pytest.raises(HTTPException) as caught:
            await validate_context_product_references(
                session,
                project,
                _definition(table_ids=orders, routine_ids=_ids(seeded, "routines", refused)),
            )
        assert caught.value.status_code == 422
        assert "routines" in str(caught.value.detail)
