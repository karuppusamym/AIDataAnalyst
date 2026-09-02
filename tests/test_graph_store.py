"""Coverage for the graph-store port (C7 / ADR-0020's 2026-08-30 amendment).

`PostgresGraphStore` is exercised against a real ORM session backed by
in-memory SQLite (the same pattern `tests/test_view_lineage_api.py` and
`tests/test_vector_store.py` use) -- genuine query execution through
`aida.unified_lineage_api._build_unified_graph`, not a mock, satisfying the
tracker's "implement it for real ... not a fake" requirement for the default
adapter. `DisabledGraphStore` and the per-organization setting are covered
directly; `Neo4jGraphStore` gets its own conformance coverage in
`tests/test_graph_store_conformance.py` (skipped cleanly where Neo4j is not
reachable, per INV-9).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import aida.graph_store as graph_store_module
import aida.models  # noqa: F401 -- registers every ORM table on Base.metadata
from aida.config import Settings
from aida.db import Base
from aida.graph_store import (
    DisabledGraphStore,
    GraphStoreOrganizationSetting,
    GraphStoreUnavailable,
    PostgresGraphStore,
    build_graph_store,
    get_organization_graph_store_backend,
    resolve_graph_store_backend,
    set_organization_graph_store_backend,
)
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
from aida.unified_lineage_api import _build_unified_graph


@pytest_asyncio.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as active:
        yield active
    await engine.dispose()


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"_env_file": None}
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class _AsyncConstant:
    """An async callable that always returns the same value, ignoring its
    arguments -- for monkeypatching a module-level async function."""

    def __init__(self, value: object) -> None:
        self._value = value

    async def __call__(self, *_args: object, **_kwargs: object) -> object:
        return self._value


async def _seed_chain(session: AsyncSession) -> tuple[DataSource, dict[str, MetadataTable]]:
    """Three tables, two declared foreign keys: a -> b -> c.

    `a` depends on `b` depends on `c` (REFERENCES convention: `source_id` is
    the dependent table). Upstream from `a` reaches `b` then `c`; downstream
    from `c` reaches `b` then `a`.
    """
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
        name="primary",
        connector_type="postgres",
        dialect="postgres",
        environment="PROD",
        network_zone="default",
        credential_reference="env://TEST_DSN",
        capabilities={},
    )
    catalog = MetadataCatalog(
        id=uuid4(), organization_id=org.id, datasource_id=datasource.id, name="bank",
        fingerprint="fp",
    )
    session.add_all([org, lob, domain, project, datasource, catalog])
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(), organization_id=org.id, catalog_id=catalog.id, name="public", fingerprint="fp"
    )
    session.add(schema)
    await session.flush()

    tables: dict[str, MetadataTable] = {}
    for name in ("a", "b", "c"):
        table = MetadataTable(
            id=uuid4(),
            organization_id=org.id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=name,
            object_type="BASE_TABLE",
            fingerprint="fp",
        )
        session.add(table)
        tables[name] = table
    await session.flush()

    session.add_all(
        [
            MetadataConstraint(
                id=uuid4(),
                organization_id=org.id,
                datasource_id=datasource.id,
                table_id=tables["a"].id,
                name="fk_a_b",
                constraint_type="FOREIGN_KEY",
                columns=["b_id"],
                referenced_table_id=tables["b"].id,
                referenced_columns=["id"],
                fingerprint="fp",
            ),
            MetadataConstraint(
                id=uuid4(),
                organization_id=org.id,
                datasource_id=datasource.id,
                table_id=tables["b"].id,
                name="fk_b_c",
                constraint_type="FOREIGN_KEY",
                columns=["c_id"],
                referenced_table_id=tables["c"].id,
                referenced_columns=["id"],
                fingerprint="fp",
            ),
        ]
    )
    await session.flush()
    return datasource, tables


# --- PostgresGraphStore: real relational reads, not a fake -------------------


async def test_lineage_impact_traverses_declared_foreign_keys(session: AsyncSession) -> None:
    datasource, tables = await _seed_chain(session)
    store = PostgresGraphStore(build_snapshot=_build_unified_graph)

    result = await store.lineage_impact(
        session, datasource, str(tables["a"].id), depth=5, node_limit=200
    )

    assert result is not None
    assert [node.node_id for node in result.upstream] == [str(tables["b"].id), str(tables["c"].id)]
    assert [node.depth for node in result.upstream] == [1, 2]
    assert result.downstream == []
    assert result.upstream_truncated is False
    assert result.downstream_truncated is False


async def test_lineage_impact_downstream_is_the_reverse_direction(session: AsyncSession) -> None:
    datasource, tables = await _seed_chain(session)
    store = PostgresGraphStore(build_snapshot=_build_unified_graph)

    result = await store.lineage_impact(
        session, datasource, str(tables["c"].id), depth=5, node_limit=200
    )

    assert result is not None
    assert [node.node_id for node in result.downstream] == [
        str(tables["b"].id), str(tables["a"].id),
    ]
    assert result.upstream == []


async def test_lineage_impact_returns_none_for_an_unknown_node(session: AsyncSession) -> None:
    datasource, _tables = await _seed_chain(session)
    store = PostgresGraphStore(build_snapshot=_build_unified_graph)

    result = await store.lineage_impact(
        session, datasource, "nonexistent-node", depth=5, node_limit=200
    )

    assert result is None


async def test_lineage_impact_respects_the_depth_cap(session: AsyncSession) -> None:
    datasource, tables = await _seed_chain(session)
    store = PostgresGraphStore(build_snapshot=_build_unified_graph)

    result = await store.lineage_impact(
        session, datasource, str(tables["a"].id), depth=1, node_limit=200
    )

    assert result is not None
    # b is one hop away; c is two hops away and depth-capped out.
    assert [node.node_id for node in result.upstream] == [str(tables["b"].id)]


async def test_lineage_impact_without_a_snapshot_builder_refuses(session: AsyncSession) -> None:
    datasource, tables = await _seed_chain(session)
    store = PostgresGraphStore()  # no build_snapshot configured

    with pytest.raises(GraphStoreUnavailable) as refused:
        await store.lineage_impact(
            session, datasource, str(tables["a"].id), depth=5, node_limit=200
        )
    assert refused.value.reason_code == "POSTGRES_GRAPH_STORE_SNAPSHOT_BUILDER_NOT_CONFIGURED"


async def test_graph_summary_counts_the_real_relational_tables(session: AsyncSession) -> None:
    datasource, _tables = await _seed_chain(session)
    store = PostgresGraphStore()

    summary = await store.graph_summary(session, datasource)

    assert summary.catalogs == 1
    assert summary.schemas == 1
    assert summary.tables == 3
    assert summary.constraints == 2
    assert summary.foreign_key_relationships == 2


async def test_graph_summary_counts_sensitive_columns(session: AsyncSession) -> None:
    from aida.models import MetadataColumn

    datasource, tables = await _seed_chain(session)
    session.add_all(
        [
            MetadataColumn(
                id=uuid4(), organization_id=datasource.organization_id, table_id=tables["a"].id,
                name="ssn", ordinal_position=1, physical_type="text", nullable=False,
                classification="PII", fingerprint="fp",
            ),
            MetadataColumn(
                id=uuid4(), organization_id=datasource.organization_id, table_id=tables["a"].id,
                name="label", ordinal_position=2, physical_type="text", nullable=False,
                classification="PUBLIC", fingerprint="fp",
            ),
        ]
    )
    await session.flush()
    store = PostgresGraphStore()

    summary = await store.graph_summary(session, datasource)

    assert summary.columns == 2
    assert summary.sensitive_columns == 1


# --- DisabledGraphStore: explicit refusal, never a silent empty result ------


async def test_disabled_store_refuses_lineage_impact(session: AsyncSession) -> None:
    datasource, tables = await _seed_chain(session)
    store = DisabledGraphStore()
    with pytest.raises(GraphStoreUnavailable) as refused:
        await store.lineage_impact(
            session, datasource, str(tables["a"].id), depth=5, node_limit=200
        )
    assert refused.value.reason_code == "GRAPH_STORE_DISABLED"


async def test_disabled_store_refuses_graph_summary(session: AsyncSession) -> None:
    datasource, _tables = await _seed_chain(session)
    store = DisabledGraphStore()
    with pytest.raises(GraphStoreUnavailable) as refused:
        await store.graph_summary(session, datasource)
    assert refused.value.reason_code == "GRAPH_STORE_DISABLED"


# --- build_graph_store factory -----------------------------------------------


def test_build_graph_store_rejects_an_unknown_backend() -> None:
    with pytest.raises(GraphStoreUnavailable) as refused:
        build_graph_store("carbon-dated", _settings())  # type: ignore[arg-type]
    assert refused.value.reason_code == "UNKNOWN_GRAPH_STORE_BACKEND"


def test_build_graph_store_returns_the_matching_adapter() -> None:
    assert build_graph_store("disabled", _settings()).name == "disabled"
    assert build_graph_store("neo4j", _settings()).name == "neo4j"
    assert build_graph_store("postgres", _settings()).name == "postgres"


# --- per-organization admin setting ------------------------------------------


async def test_a_new_organization_has_no_setting_and_resolves_to_the_process_default(
    session: AsyncSession,
) -> None:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()

    assert await get_organization_graph_store_backend(session, org.id) is None
    assert await resolve_graph_store_backend(session, org.id, _settings()) == "postgres"


async def test_setting_the_backend_persists_and_resolves(session: AsyncSession) -> None:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()

    await set_organization_graph_store_backend(session, org.id, "disabled")

    assert await get_organization_graph_store_backend(session, org.id) == "disabled"
    assert await resolve_graph_store_backend(session, org.id, _settings()) == "disabled"


async def test_setting_the_backend_twice_updates_rather_than_duplicates(
    session: AsyncSession,
) -> None:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()

    await set_organization_graph_store_backend(session, org.id, "neo4j")
    await set_organization_graph_store_backend(session, org.id, "disabled")

    rows = (
        await session.scalars(
            select(GraphStoreOrganizationSetting).where(
                GraphStoreOrganizationSetting.organization_id == org.id
            )
        )
    ).all()
    assert len(rows) == 1
    assert await get_organization_graph_store_backend(session, org.id) == "disabled"


async def test_set_organization_graph_store_backend_rejects_an_unknown_value(
    session: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="unknown graph store backend"):
        await set_organization_graph_store_backend(
            session, uuid4(), "carbon-dated"  # type: ignore[arg-type]
        )


async def test_neo4j_is_uncertified_and_falls_back_to_postgres_until_the_operator_opts_in(
    session: AsyncSession,
) -> None:
    """INV-9: an organization may request `neo4j`, but the process-wide
    `lineage_neo4j_read_enabled` flag (default off, since E5 has not run) is
    what actually certifies it. Requesting it alone does not serve it."""
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    await set_organization_graph_store_backend(session, org.id, "neo4j")

    assert await resolve_graph_store_backend(
        session, org.id, _settings(lineage_neo4j_read_enabled=False)
    ) == "postgres"
    assert await resolve_graph_store_backend(
        session, org.id, _settings(lineage_neo4j_read_enabled=True)
    ) == "neo4j"


async def test_the_database_itself_refuses_an_invalid_backend(session: AsyncSession) -> None:
    """The migration's `CHECK` constraint is the first line of defense -- a raw
    insert that bypasses `set_organization_graph_store_backend`'s own
    `ValueError` guard still cannot reach the database with a value outside
    the three certified backends."""
    from sqlalchemy.exc import IntegrityError

    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    session.add(GraphStoreOrganizationSetting(organization_id=org.id, backend="quantum-graph"))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_an_unrecognised_configured_backend_falls_back_to_postgres_rather_than_raising(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Defense in depth alongside the database `CHECK` constraint proven above:
    if an unrecognised value ever reaches this function regardless -- a row
    from before the constraint existed, a future backend name an older
    deployment does not know -- it degrades to the certified default rather
    than raising or silently promising an unknown backend."""
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()
    monkeypatch.setattr(
        graph_store_module,
        "get_organization_graph_store_backend",
        _AsyncConstant("quantum-graph"),
    )

    assert await resolve_graph_store_backend(session, org.id, _settings()) == "postgres"


async def test_the_process_wide_default_is_used_when_no_organization_row_exists(
    session: AsyncSession,
) -> None:
    org = Organization(id=uuid4(), name="Bank", slug=f"bank-{uuid4().hex[:8]}")
    session.add(org)
    await session.flush()

    assert await resolve_graph_store_backend(
        session, org.id, _settings(graph_store_backend="disabled")
    ) == "disabled"
