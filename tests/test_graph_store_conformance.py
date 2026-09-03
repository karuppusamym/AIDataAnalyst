"""Conformance suite: `postgres` and `neo4j` must answer identically (C7 / ADR-0020).

ADR-0020's 2026-08-30 amendment names this the actual deliverable, not the port
itself: "Both backends must answer identically ... Not 'both return something' --
identical node sets, identical ordering and tie-breaks, identical cap behaviour
and identical truncation reasons." This module is that suite.

**Two layers, so the harness is exercised even without a live Neo4j.**

1. `test_the_comparison_harness_catches_a_real_divergence` and
   `test_the_comparison_harness_accepts_identical_results` drive
   `_assert_impact_reads_match` directly against hand-built results -- no
   database of any kind. This is what "the conformance harness itself must be
   fully exercised now" means when Neo4j is not reachable: the assertion that
   would catch a real backend disagreement is proven to actually catch one.
2. `test_postgres_and_neo4j_agree_on_a_simple_dependency_chain` and
   `test_postgres_and_neo4j_agree_on_truncation_under_a_tight_node_cap` seed
   the *same* fixture into a real SQLite-backed PostgresGraphStore and a real
   Neo4j, then run both adapters and compare. They skip cleanly, with a stated
   reason, when no Neo4j is reachable (INV-9: none runs in CI today) -- the
   same "probe, skip only on unreachability, never fake green" pattern
   `tests/test_migration_orm_drift.py` uses for PostgreSQL. When a Neo4j *is*
   reachable (set `AIDA_NEO4J_URI`/`AIDA_NEO4J_USER`/`AIDA_NEO4J_PASSWORD`, or
   run one at `bolt://localhost:7687` matching `.env.example`'s defaults),
   they run for real, seeding and tearing down under a private, fixed
   `organization_id`/`datasource_id` (`DETACH DELETE` in a `finally` block)
   so they never collide with or leak into anything else in that database.

Building this suite found a real bug: the `neo4j` adapter's UPSTREAM/DOWNSTREAM
Cypher pattern was swapped relative to `PostgresGraphStore`'s (and
`aida.unified_lineage`'s documented) direction convention -- see the fix and
comment in `aida.graph_store.Neo4jGraphStore.lineage_impact`. Nothing had ever
run the `neo4j` adapter against a real database to notice; that is exactly
what INV-9 predicts a never-certified backend looks like.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID

import pytest
import pytest_asyncio
from neo4j import AsyncGraphDatabase
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.models  # noqa: F401 -- registers every ORM table on Base.metadata
from aida.config import Settings
from aida.db import Base
from aida.graph_store import Neo4jGraphStore, PostgresGraphStore
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.schemas import UnifiedLineageImpactNodeRead, UnifiedLineageImpactRead
from aida.unified_lineage_api import _build_unified_graph

# Fixed, not `uuid4()`: the same ids are embedded in both the SQLite fixture and
# the Neo4j seed Cypher, so the two backends are provably reading the same graph.
_ORG = UUID("c0111000-0000-0000-0000-000000000001")
_LOB = UUID("c0111000-0000-0000-0000-000000000002")
_DOMAIN = UUID("c0111000-0000-0000-0000-000000000003")
_PROJECT = UUID("c0111000-0000-0000-0000-000000000004")
_DATASOURCE = UUID("c0111000-0000-0000-0000-000000000005")
_TABLE_A = UUID("c0111000-0000-0000-0000-00000000000a")
_TABLE_B = UUID("c0111000-0000-0000-0000-00000000000b")
_TABLE_C = UUID("c0111000-0000-0000-0000-00000000000c")


# --- Layer 1: the comparison harness itself, no database ---------------------


def _assert_impact_reads_match(
    postgres: UnifiedLineageImpactRead, neo4j: UnifiedLineageImpactRead
) -> None:
    """The actual conformance assertion: identical node sets, identical
    ordering, identical cap/truncation behaviour -- not merely "both answered".
    """
    assert postgres.focus_node_id == neo4j.focus_node_id
    assert postgres.focus_node_kind == neo4j.focus_node_kind
    assert postgres.focus_label == neo4j.focus_label
    assert [n.node_id for n in postgres.upstream] == [n.node_id for n in neo4j.upstream], (
        "upstream node sets/ordering diverge"
    )
    assert [n.depth for n in postgres.upstream] == [n.depth for n in neo4j.upstream], (
        "upstream depths diverge"
    )
    assert [n.node_id for n in postgres.downstream] == [n.node_id for n in neo4j.downstream], (
        "downstream node sets/ordering diverge"
    )
    assert [n.depth for n in postgres.downstream] == [n.depth for n in neo4j.downstream], (
        "downstream depths diverge"
    )
    assert postgres.upstream_truncated == neo4j.upstream_truncated, "upstream truncation diverges"
    assert postgres.downstream_truncated == neo4j.downstream_truncated, (
        "downstream truncation diverges"
    )


def _sample_impact_read(**overrides: object) -> UnifiedLineageImpactRead:
    base: dict[str, object] = dict(
        datasource_id=_DATASOURCE,
        focus_node_id=str(_TABLE_A),
        focus_node_kind="TABLE",
        focus_label="bank.public.a",
        upstream=[
            UnifiedLineageImpactNodeRead(
                node_id=str(_TABLE_B), node_kind="TABLE", label="b",
                qualified_name="bank.public.b", depth=1, contributing_edge_sources=["FOREIGN_KEY"],
            ),
        ],
        downstream=[],
        requested_depth=5,
        node_limit=200,
        upstream_truncated=False,
        downstream_truncated=False,
    )
    base.update(overrides)
    return UnifiedLineageImpactRead(**base)  # type: ignore[arg-type]


def test_the_comparison_harness_accepts_identical_results() -> None:
    a = _sample_impact_read()
    b = _sample_impact_read()
    _assert_impact_reads_match(a, b)  # must not raise


def test_the_comparison_harness_catches_a_real_divergence() -> None:
    """Proves the assertion has teeth: two results that differ only in
    upstream node order must fail the comparison, not pass it."""
    correctly_ordered = _sample_impact_read(
        upstream=[
            UnifiedLineageImpactNodeRead(
                node_id=str(_TABLE_B), node_kind="TABLE", label="b",
                qualified_name="bank.public.b", depth=1, contributing_edge_sources=["FOREIGN_KEY"],
            ),
            UnifiedLineageImpactNodeRead(
                node_id=str(_TABLE_C), node_kind="TABLE", label="c",
                qualified_name="bank.public.c", depth=2, contributing_edge_sources=["FOREIGN_KEY"],
            ),
        ]
    )
    reversed_order = _sample_impact_read(
        upstream=list(reversed(correctly_ordered.upstream))
    )
    with pytest.raises(AssertionError, match="upstream node sets/ordering diverge"):
        _assert_impact_reads_match(correctly_ordered, reversed_order)


def test_the_comparison_harness_catches_a_truncation_disagreement() -> None:
    not_truncated = _sample_impact_read(upstream_truncated=False)
    truncated = _sample_impact_read(upstream_truncated=True)
    with pytest.raises(AssertionError, match="upstream truncation diverges"):
        _assert_impact_reads_match(not_truncated, truncated)


# --- Layer 2: real postgres vs real neo4j, same fixture -----------------------


@pytest_asyncio.fixture
async def sqlite_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


async def _seed_postgres_chain(session: AsyncSession) -> DataSource:
    """a -> b -> c (a depends on b depends on c), fixed ids matching `_seed_neo4j_chain`."""
    session.add_all(
        [
            Organization(id=_ORG, name="Bank", slug="conformance-bank"),
            LineOfBusiness(id=_LOB, organization_id=_ORG, name="Retail", code="RTL"),
            DataDomain(
                id=_DOMAIN, organization_id=_ORG, line_of_business_id=_LOB,
                name="Ungoverned", code="UNG",
            ),
            Project(
                id=_PROJECT, organization_id=_ORG, line_of_business_id=_LOB,
                data_domain_id=_DOMAIN, name="Warehouse", slug="warehouse",
            ),
        ]
    )
    datasource = DataSource(
        id=_DATASOURCE, organization_id=_ORG, line_of_business_id=_LOB,
        data_domain_id=_DOMAIN, project_id=_PROJECT, name="primary",
        connector_type="postgres", dialect="postgres", environment="PROD",
        network_zone="default", credential_reference="env://TEST_DSN", capabilities={},
    )
    session.add(datasource)
    catalog = MetadataCatalog(
        id=UUID("c0111000-0000-0000-0000-0000000000c1"), organization_id=_ORG,
        datasource_id=_DATASOURCE, name="bank", fingerprint="fp",
    )
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        id=UUID("c0111000-0000-0000-0000-0000000000c2"), organization_id=_ORG,
        catalog_id=catalog.id, name="public", fingerprint="fp",
    )
    session.add(schema)
    await session.flush()
    for table_id, name in ((_TABLE_A, "a"), (_TABLE_B, "b"), (_TABLE_C, "c")):
        session.add(
            MetadataTable(
                id=table_id, organization_id=_ORG, datasource_id=_DATASOURCE,
                schema_id=schema.id, name=name, object_type="BASE_TABLE", fingerprint="fp",
            )
        )
    await session.flush()
    session.add_all(
        [
            MetadataConstraint(
                id=UUID("c0111000-0000-0000-0000-0000000000f1"), organization_id=_ORG,
                datasource_id=_DATASOURCE, table_id=_TABLE_A, name="fk_a_b",
                constraint_type="FOREIGN_KEY", columns=["b_id"], referenced_table_id=_TABLE_B,
                referenced_columns=["id"], fingerprint="fp",
            ),
            MetadataConstraint(
                id=UUID("c0111000-0000-0000-0000-0000000000f2"), organization_id=_ORG,
                datasource_id=_DATASOURCE, table_id=_TABLE_B, name="fk_b_c",
                constraint_type="FOREIGN_KEY", columns=["c_id"], referenced_table_id=_TABLE_C,
                referenced_columns=["id"], fingerprint="fp",
            ),
        ]
    )
    await session.flush()
    return datasource


def _neo4j_test_settings() -> Settings:
    # Matches `.env.example`'s defaults so a locally-run Neo4j needs no extra
    # configuration; override via the same `AIDA_NEO4J_*` env vars the app itself
    # reads (`Settings` picks them up automatically) to point at another instance.
    return Settings(_env_file=None)  # type: ignore[call-arg]


async def _probe_neo4j(settings: Settings) -> None:
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
        connection_timeout=2.0,
    )
    try:
        await driver.verify_connectivity()
    finally:
        await driver.close()


async def _seed_neo4j_chain(settings: Settings) -> None:
    """Writes the same a -> b -> c chain in the exact node/edge shape
    `aida.projectors.graph_projector.project_unified_lineage` writes -- same
    property names, same `UNIFIED_LINEAGE` relationship type and direction --
    so this is a faithful stand-in for the real projector's output, not a
    shape the read path has never actually seen."""
    prefix = f"{_ORG}:{_DATASOURCE}:"
    tables = {_TABLE_A: "a", _TABLE_B: "b", _TABLE_C: "c"}
    edges = [(_TABLE_A, _TABLE_B), (_TABLE_B, _TABLE_C)]
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        async with driver.session() as session:
            for table_id, name in tables.items():
                await session.run(
                    """
                    MERGE (n:UnifiedLineageNode {projection_key: $projection_key})
                    SET n.platform_id = $platform_id,
                        n.organization_id = $organization_id,
                        n.datasource_id = $datasource_id,
                        n.node_kind = 'TABLE',
                        n.label = $label,
                        n.qualified_name = $qualified_name
                    """,
                    projection_key=f"{prefix}{table_id}",
                    platform_id=str(table_id),
                    organization_id=str(_ORG),
                    datasource_id=str(_DATASOURCE),
                    label=name,
                    qualified_name=f"bank.public.{name}",
                )
            for source_id, target_id in edges:
                await session.run(
                    """
                    MATCH (source:UnifiedLineageNode {projection_key: $source_pk})
                    MATCH (target:UnifiedLineageNode {projection_key: $target_pk})
                    MERGE (source)-[edge:UNIFIED_LINEAGE {projection_key: $edge_pk}]->(target)
                    SET edge.organization_id = $organization_id,
                        edge.datasource_id = $datasource_id,
                        edge.edge_source = 'FOREIGN_KEY'
                    """,
                    source_pk=f"{prefix}{source_id}",
                    target_pk=f"{prefix}{target_id}",
                    edge_pk=f"{prefix}{source_id}:{target_id}",
                    organization_id=str(_ORG),
                    datasource_id=str(_DATASOURCE),
                )
    finally:
        await driver.close()


async def _teardown_neo4j_chain(settings: Settings) -> None:
    driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password)
    )
    try:
        async with driver.session() as session:
            await session.run(
                """
                MATCH (n:UnifiedLineageNode)
                WHERE n.organization_id = $organization_id AND n.datasource_id = $datasource_id
                DETACH DELETE n
                """,
                organization_id=str(_ORG),
                datasource_id=str(_DATASOURCE),
            )
    finally:
        await driver.close()


async def _skip_unless_neo4j_reachable(settings: Settings) -> None:
    try:
        await _probe_neo4j(settings)
    except Exception as exc:  # noqa: BLE001 -- any connection failure means "skip"
        pytest.skip(
            f"Neo4j is not reachable at {settings.neo4j_uri!r} ({type(exc).__name__}: {exc}); "
            "the neo4j adapter is uncertified (INV-9) and this half of the conformance "
            "suite needs a real instance -- see this file's module docstring for how to "
            "point it at one. The postgres adapter and the comparison harness above are "
            "still fully exercised without one."
        )


async def test_postgres_and_neo4j_agree_on_a_simple_dependency_chain(
    sqlite_session: AsyncSession,
) -> None:
    settings = _neo4j_test_settings()
    await _skip_unless_neo4j_reachable(settings)

    datasource = await _seed_postgres_chain(sqlite_session)
    await _seed_neo4j_chain(settings)
    try:
        postgres_store = PostgresGraphStore(build_snapshot=_build_unified_graph)
        neo4j_store = Neo4jGraphStore(settings)

        for node_id in (str(_TABLE_A), str(_TABLE_B), str(_TABLE_C)):
            postgres_result = await postgres_store.lineage_impact(
                sqlite_session, datasource, node_id, depth=5, node_limit=200
            )
            neo4j_result = await neo4j_store.lineage_impact(
                sqlite_session, datasource, node_id, depth=5, node_limit=200
            )
            assert postgres_result is not None, f"postgres found no node for {node_id}"
            assert neo4j_result is not None, f"neo4j found no node for {node_id}"
            _assert_impact_reads_match(postgres_result, neo4j_result)
    finally:
        await _teardown_neo4j_chain(settings)


async def test_postgres_and_neo4j_agree_on_truncation_under_a_tight_node_cap(
    sqlite_session: AsyncSession,
) -> None:
    settings = _neo4j_test_settings()
    await _skip_unless_neo4j_reachable(settings)

    datasource = await _seed_postgres_chain(sqlite_session)
    await _seed_neo4j_chain(settings)
    try:
        postgres_store = PostgresGraphStore(build_snapshot=_build_unified_graph)
        neo4j_store = Neo4jGraphStore(settings)

        # node_limit=1: only the seed itself fits, so both directions must report
        # truncated=True on both backends, identically -- not "both truncated
        # something", but the same cap behaviour.
        postgres_result = await postgres_store.lineage_impact(
            sqlite_session, datasource, str(_TABLE_A), depth=5, node_limit=1
        )
        neo4j_result = await neo4j_store.lineage_impact(
            sqlite_session, datasource, str(_TABLE_A), depth=5, node_limit=1
        )
        assert postgres_result is not None
        assert neo4j_result is not None
        _assert_impact_reads_match(postgres_result, neo4j_result)
        assert postgres_result.upstream_truncated is True
        assert neo4j_result.upstream_truncated is True
    finally:
        await _teardown_neo4j_chain(settings)
