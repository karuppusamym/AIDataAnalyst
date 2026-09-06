"""The graph projection's read path is bounded, complete, and tenant-scoped.

Three separate claims, and they need separate tests because a chunked reader can
fail each one independently: it can bound memory and lose rows, it can return
every row and page across a tenant boundary, or it can be correct and still hand
the writer a child before its parent.

Runs against SQLite built from the ORM, like the rest of the suite; the
wall-clock and peak-memory comparison against the previous whole-estate
implementation lives in `scripts/scale_harness/gp1_measure_projection_memory.py`,
which needs a large synthetic estate and is too slow for a unit test.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.graph_projection import (
    DEFAULT_PROJECTION_CHUNK_SIZE,
    ProjectionChunk,
    iter_projection_chunks,
    resolve_chunk_size,
)
from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataConstraint,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from aida.projection_metrics import PROJECTION_LEVELS

_FINGERPRINT = "f" * 64


async def _seed(session: AsyncSession, *, tables: int, columns_per_table: int) -> tuple[UUID, UUID]:
    """One datasource with a known row count, plus a second datasource in a
    second organization that must never appear in the first one's chunks."""
    organization_id, other_organization_id = uuid4(), uuid4()
    ids: dict[str, UUID] = {}
    for label, org in (("a", organization_id), ("b", other_organization_id)):
        lob_id, domain_id, project_id, datasource_id, catalog_id, schema_id = (
            uuid4() for _ in range(6)
        )
        ids[f"{label}_datasource"] = datasource_id
        session.add(Organization(id=org, name=f"org-{label}", slug=f"org-{label}"))
        session.add(LineOfBusiness(id=lob_id, organization_id=org, name="lob", code=f"L{label}"))
        session.add(
            DataDomain(
                id=domain_id,
                organization_id=org,
                line_of_business_id=lob_id,
                name="domain",
                code=f"D{label}",
            )
        )
        session.add(
            Project(
                id=project_id,
                organization_id=org,
                line_of_business_id=lob_id,
                data_domain_id=domain_id,
                name="project",
                slug=f"project-{label}",
            )
        )
        session.add(
            DataSource(
                id=datasource_id,
                organization_id=org,
                line_of_business_id=lob_id,
                data_domain_id=domain_id,
                project_id=project_id,
                name=f"source-{label}",
                connector_type="postgres",
                dialect="postgres",
                environment="TEST",
                credential_reference="vault://x",
                status="ACTIVE",
            )
        )
        session.add(
            MetadataCatalog(
                id=catalog_id,
                organization_id=org,
                datasource_id=datasource_id,
                name="warehouse",
                status="ACTIVE",
                fingerprint=_FINGERPRINT,
            )
        )
        session.add(
            MetadataSchema(
                id=schema_id,
                organization_id=org,
                catalog_id=catalog_id,
                name="finance",
                status="ACTIVE",
                fingerprint=_FINGERPRINT,
            )
        )
        count = tables if label == "a" else 2
        for table_index in range(count):
            table_id = uuid4()
            session.add(
                MetadataTable(
                    id=table_id,
                    organization_id=org,
                    datasource_id=datasource_id,
                    schema_id=schema_id,
                    name=f"table_{table_index}",
                    object_type="BASE_TABLE",
                    status="ACTIVE",
                    fingerprint=_FINGERPRINT,
                )
            )
            for column_index in range(columns_per_table):
                session.add(
                    MetadataColumn(
                        id=uuid4(),
                        organization_id=org,
                        table_id=table_id,
                        name=f"column_{column_index}",
                        ordinal_position=column_index + 1,
                        physical_type="text",
                        nullable=True,
                        classification="INTERNAL",
                        status="ACTIVE",
                        fingerprint=_FINGERPRINT,
                    )
                )
            session.add(
                MetadataConstraint(
                    id=uuid4(),
                    organization_id=org,
                    datasource_id=datasource_id,
                    table_id=table_id,
                    name=f"pk_{table_index}",
                    constraint_type="PRIMARY_KEY",
                    columns=["column_0"],
                    referenced_table_id=None,
                    referenced_columns=[],
                    status="ACTIVE",
                    fingerprint=_FINGERPRINT,
                )
            )
    await session.commit()
    return organization_id, ids["a_datasource"]


@pytest.fixture
async def projection_session() -> AsyncIterator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with factory() as session:
        yield session
    await engine.dispose()


async def _collect(
    session: AsyncSession, datasource_id: UUID, organization_id: UUID, *, chunk_size: int
) -> list[ProjectionChunk]:
    return [
        chunk
        async for chunk in iter_projection_chunks(
            session, datasource_id, organization_id, chunk_size=chunk_size
        )
    ]


async def test_every_row_is_yielded_regardless_of_chunk_size(
    projection_session: AsyncSession,
) -> None:
    """Paging must not lose the boundary row.

    An off-by-one in a keyset cursor drops exactly one row per page and is
    invisible on any estate that fits in a single chunk -- which is every estate
    the rest of the suite uses. Running the same fixture at four chunk sizes,
    including ones that divide the row count exactly and ones that do not, is
    what makes that failure reachable.
    """
    organization_id, datasource_id = await _seed(projection_session, tables=10, columns_per_table=5)
    expected = {"catalogs": 1, "schemas": 1, "tables": 10, "columns": 50, "constraints": 10}

    for chunk_size in (50, 10, 7, 1):
        chunks = await _collect(
            projection_session, datasource_id, organization_id, chunk_size=chunk_size
        )
        counts: dict[str, int] = {}
        for chunk in chunks:
            counts[chunk.level] = counts.get(chunk.level, 0) + len(chunk.rows)
            assert len(chunk.rows) <= chunk_size, (
                f"chunk of {len(chunk.rows)} rows exceeds the {chunk_size} bound; "
                "memory is no longer bounded by the chunk size"
            )
        assert counts == expected, f"row loss at chunk_size={chunk_size}"

        platform_ids = [row["platform_id"] for chunk in chunks for row in chunk.rows]
        assert len(platform_ids) == len(set(platform_ids)), (
            f"the same row was yielded twice at chunk_size={chunk_size}; a keyset "
            "cursor that does not advance re-reads its own page"
        )


async def test_levels_arrive_parents_before_children(
    projection_session: AsyncSession,
) -> None:
    """The writer MERGEs a child by MATCHing its already-written parent, so a
    chunk order that interleaves levels would produce a projection with silently
    missing edges rather than an error."""
    organization_id, datasource_id = await _seed(projection_session, tables=4, columns_per_table=3)
    chunks = await _collect(projection_session, datasource_id, organization_id, chunk_size=2)

    order: list[str] = []
    for chunk in chunks:
        if not order or order[-1] != chunk.level:
            order.append(chunk.level)
    assert order == list(PROJECTION_LEVELS)
    assert len(order) == len(set(order)), "a level was revisited after a later level began"


async def test_chunk_sequence_numbers_are_per_level(
    projection_session: AsyncSession,
) -> None:
    """`sequence` says where a rebuild got to *within* a level; a global counter
    would make a resumed rebuild's progress unreadable."""
    organization_id, datasource_id = await _seed(projection_session, tables=6, columns_per_table=2)
    chunks = await _collect(projection_session, datasource_id, organization_id, chunk_size=2)

    by_level: dict[str, list[int]] = {}
    for chunk in chunks:
        by_level.setdefault(chunk.level, []).append(chunk.sequence)
    for level, sequences in by_level.items():
        assert sequences == list(range(len(sequences))), f"{level} sequence is not 0-based dense"


async def test_chunks_never_cross_a_tenant_or_datasource_boundary(
    projection_session: AsyncSession,
) -> None:
    """INV-5 for the projector's read path.

    Every level is reached by joining up through the catalog hierarchy to the
    requested datasource, so a second organization's rows can only appear if
    that join is wrong -- which is exactly the defect a projection would carry
    into a shared graph and never surface as an error.
    """
    organization_id, datasource_id = await _seed(projection_session, tables=3, columns_per_table=2)
    chunks = await _collect(projection_session, datasource_id, organization_id, chunk_size=100)

    rows = [row for chunk in chunks for row in chunk.rows]
    assert rows
    assert {row["organization_id"] for row in rows} == {str(organization_id)}
    assert {row["datasource_id"] for row in rows} == {str(datasource_id)}


async def test_every_row_carries_the_scope_the_deletion_sweep_needs(
    projection_session: AsyncSession,
) -> None:
    """Deletion reconciliation finds retired nodes by "same tenant, same
    datasource, older generation". A level whose rows omit `datasource_id`
    could never be swept, so its nodes would outlive their source rows -- the
    additive-world assumption this whole change exists to remove."""
    organization_id, datasource_id = await _seed(projection_session, tables=2, columns_per_table=2)
    chunks = await _collect(projection_session, datasource_id, organization_id, chunk_size=100)

    for chunk in chunks:
        for row in chunk.rows:
            assert row.get("organization_id"), f"{chunk.level} row has no organization_id: {row}"
            assert row.get("datasource_id"), f"{chunk.level} row has no datasource_id: {row}"
            assert row.get("platform_id"), f"{chunk.level} row has no platform_id: {row}"


async def test_tenancy_path_is_merged_into_every_level(
    projection_session: AsyncSession,
) -> None:
    """ADR-0017 SS2: a domain-scoped traversal filters before it walks edges,
    which it can only do if every node carries the domain."""
    organization_id, datasource_id = await _seed(projection_session, tables=2, columns_per_table=1)
    path: dict[str, Any] = {"data_domain_id": "dom-1", "project_id": "prj-1"}
    chunks = [
        chunk
        async for chunk in iter_projection_chunks(
            projection_session,
            datasource_id,
            organization_id,
            tenancy_path=path,
            chunk_size=100,
        )
    ]
    for chunk in chunks:
        for row in chunk.rows:
            assert row["data_domain_id"] == "dom-1"
            assert row["project_id"] == "prj-1"


def test_chunk_size_is_always_bounded() -> None:
    """The knob can be tuned but can never be turned off: an absent, malformed
    or absurd value still yields a finite chunk."""
    assert resolve_chunk_size({}) == DEFAULT_PROJECTION_CHUNK_SIZE
    assert resolve_chunk_size({"AIDA_GRAPH_PROJECTION_CHUNK_ROWS": "not-a-number"}) == (
        DEFAULT_PROJECTION_CHUNK_SIZE
    )
    assert resolve_chunk_size({"AIDA_GRAPH_PROJECTION_CHUNK_ROWS": "0"}) == 50
    assert resolve_chunk_size({"AIDA_GRAPH_PROJECTION_CHUNK_ROWS": "-1"}) == 50
    assert resolve_chunk_size({"AIDA_GRAPH_PROJECTION_CHUNK_ROWS": "999999999"}) == 50_000
    assert resolve_chunk_size({"AIDA_GRAPH_PROJECTION_CHUNK_ROWS": "2500"}) == 2_500


def test_projector_tuning_env_names_are_not_settings_typos() -> None:
    """`Settings.reject_unrecognized_aida_env_vars` refuses any `AIDA_*` name
    that is a close match of a real setting. These three deliberately are not,
    but "deliberately" is worth asserting -- a renamed setting could make one of
    them a near-match later and take the whole process down at startup.
    """
    import difflib

    from aida.config import Settings
    from aida.graph_projection import (
        MAX_BUFFERED_EVENTS_ENV,
        PROJECTION_CHUNK_SIZE_ENV,
        TENANT_EVENT_BUDGET_ENV,
    )

    known = {f"AIDA_{name.upper()}" for name in Settings.model_fields}
    for env_name in (PROJECTION_CHUNK_SIZE_ENV, TENANT_EVENT_BUDGET_ENV, MAX_BUFFERED_EVENTS_ENV):
        assert env_name not in known
        assert not difflib.get_close_matches(env_name, sorted(known), n=1, cutoff=0.84), (
            f"{env_name} is close enough to a real setting name that "
            "Settings would reject it as a typo"
        )
