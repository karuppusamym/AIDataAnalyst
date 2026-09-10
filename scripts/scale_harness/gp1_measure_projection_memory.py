#!/usr/bin/env python3
"""GP-1 scale harness -- measure the graph projection's read path, before and after.

The 2026-09-05 review's graph-projection row is explicit that chunking "is not
a blind change" and wants a measured large-source rebuild. This is that
measurement, and it is deliberately arranged so the improvement is a number
rather than a claim: both implementations are run against the *same* synthetic
estate in the same process, and both are measured the same way.

What is measured
----------------
* **Peak Python heap** via `tracemalloc`, reset immediately before each run.
  This is the quantity the review's finding is about -- "`load_projection`
  materializes catalogs/schemas/tables/columns" -- and `tracemalloc` measures
  exactly the Python-object allocation that materialisation causes. It does
  **not** measure the driver's buffers or RSS; a claim about RSS would need a
  platform-specific probe and a real Postgres, neither of which this harness
  has.
* **Wall-clock seconds** per run, median of `--repeats`.

What this is not
----------------
This runs against SQLite, in-process, with the graph write replaced by a
counting sink. It measures the *read and materialisation* path -- the half the
review flagged -- not Neo4j ingestion, not network, and not a production
Postgres query plan. The E5 projection-rebuild drill against a real Neo4j
remains open and this harness does not close it.

The "before" implementation below is a verbatim copy of the `load_projection`
this change removed. It lives here, in a measurement script, rather than in
`src/aida`, precisely so that nothing can accidentally call it again: its only
remaining purpose is to be the number the new path is compared against.

Usage
-----
    python scripts/scale_harness/gp1_measure_projection_memory.py \
        --tables 5000 --columns-per-table 40 --schemas 50
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import tracemalloc
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from aida.db import Base
from aida.graph_projection import DEFAULT_PROJECTION_CHUNK_SIZE, iter_projection_chunks
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

_FINGERPRINT = "f" * 64


# ---------------------------------------------------------------------------
# The pre-change implementation, kept only to be measured against.
# ---------------------------------------------------------------------------


async def load_projection_before(
    session: AsyncSession, datasource_id: UUID, organization_id: UUID
) -> dict[str, list[dict[str, Any]]]:
    """`aida.projectors.graph_projector.load_projection` as it stood before this
    change, with its `session_factory()` call replaced by an injected session so
    both paths read the same database. Nothing else is altered."""
    datasource = await session.get(DataSource, datasource_id)
    tenancy_path = (
        {
            "line_of_business_id": str(datasource.line_of_business_id),
            "data_domain_id": str(datasource.data_domain_id),
            "project_id": str(datasource.project_id),
        }
        if datasource is not None
        else {}
    )
    catalogs = (
        await session.scalars(
            select(MetadataCatalog).where(
                MetadataCatalog.datasource_id == datasource_id,
                MetadataCatalog.organization_id == organization_id,
            )
        )
    ).all()
    catalog_ids = [catalog.id for catalog in catalogs]
    schemas = (
        await session.scalars(
            select(MetadataSchema).where(
                MetadataSchema.catalog_id.in_(catalog_ids),
                MetadataSchema.organization_id == organization_id,
            )
        )
    ).all()
    schema_ids = [schema.id for schema in schemas]
    tables = (
        await session.scalars(
            select(MetadataTable).where(
                MetadataTable.schema_id.in_(schema_ids),
                MetadataTable.organization_id == organization_id,
            )
        )
    ).all()
    table_ids = [table.id for table in tables]
    columns = (
        await session.scalars(
            select(MetadataColumn).where(
                MetadataColumn.table_id.in_(table_ids),
                MetadataColumn.organization_id == organization_id,
            )
        )
    ).all()
    constraints = (
        await session.scalars(
            select(MetadataConstraint).where(
                MetadataConstraint.table_id.in_(table_ids),
                MetadataConstraint.organization_id == organization_id,
            )
        )
    ).all()

    return {
        "catalogs": [
            {
                "platform_id": str(item.id),
                "organization_id": str(organization_id),
                "datasource_id": str(datasource_id),
                "name": item.name,
                "status": item.status,
                **tenancy_path,
            }
            for item in catalogs
        ],
        "schemas": [
            {
                "platform_id": str(item.id),
                "organization_id": str(organization_id),
                "catalog_id": str(item.catalog_id),
                "name": item.name,
                "status": item.status,
                **tenancy_path,
            }
            for item in schemas
        ],
        "tables": [
            {
                "platform_id": str(item.id),
                "organization_id": str(organization_id),
                "schema_id": str(item.schema_id),
                "name": item.name,
                "object_type": item.object_type,
                "status": item.status,
                **tenancy_path,
            }
            for item in tables
        ],
        "columns": [
            {
                "platform_id": str(item.id),
                "organization_id": str(organization_id),
                "table_id": str(item.table_id),
                "name": item.name,
                "ordinal_position": item.ordinal_position,
                "physical_type": item.physical_type,
                "classification": item.classification,
                "status": item.status,
                **tenancy_path,
            }
            for item in columns
        ],
        "constraints": [
            {
                "platform_id": str(item.id),
                "organization_id": str(organization_id),
                "table_id": str(item.table_id),
                "name": item.name,
                "constraint_type": item.constraint_type,
                "columns": item.columns,
                "referenced_table_id": (
                    str(item.referenced_table_id) if item.referenced_table_id else None
                ),
                "referenced_columns": item.referenced_columns,
                "status": item.status,
                **tenancy_path,
            }
            for item in constraints
        ],
    }


# ---------------------------------------------------------------------------
# Synthetic estate
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Estate:
    organization_id: UUID
    datasource_id: UUID
    rows: int


async def seed_estate(
    session: AsyncSession, *, tables: int, columns_per_table: int, schemas: int
) -> Estate:
    """A value-free synthetic estate (ADR-0014): shapes and counts only, no
    content resembling anything real."""
    organization_id = uuid4()
    lob_id, domain_id, project_id, datasource_id = (uuid4() for _ in range(4))
    session.add(Organization(id=organization_id, name="gp1-harness", slug="gp1-harness"))
    session.add(LineOfBusiness(id=lob_id, organization_id=organization_id, name="lob", code="LOB"))
    session.add(
        DataDomain(
            id=domain_id,
            organization_id=organization_id,
            line_of_business_id=lob_id,
            name="domain",
            code="DOM",
        )
    )
    session.add(
        Project(
            id=project_id,
            organization_id=organization_id,
            line_of_business_id=lob_id,
            data_domain_id=domain_id,
            name="project",
            slug="project",
        )
    )
    session.add(
        DataSource(
            id=datasource_id,
            organization_id=organization_id,
            line_of_business_id=lob_id,
            data_domain_id=domain_id,
            project_id=project_id,
            name="gp1-source",
            connector_type="postgres",
            dialect="postgres",
            environment="TEST",
            credential_reference="vault://gp1",
            status="ACTIVE",
        )
    )
    catalog_id = uuid4()
    session.add(
        MetadataCatalog(
            id=catalog_id,
            organization_id=organization_id,
            datasource_id=datasource_id,
            name="warehouse",
            status="ACTIVE",
            fingerprint=_FINGERPRINT,
        )
    )
    await session.flush()

    schema_ids = [uuid4() for _ in range(schemas)]
    for index, schema_id in enumerate(schema_ids):
        session.add(
            MetadataSchema(
                id=schema_id,
                organization_id=organization_id,
                catalog_id=catalog_id,
                name=f"schema_{index}",
                status="ACTIVE",
                fingerprint=_FINGERPRINT,
            )
        )
    await session.flush()

    rows = 1 + schemas
    for table_index in range(tables):
        table_id = uuid4()
        session.add(
            MetadataTable(
                id=table_id,
                organization_id=organization_id,
                datasource_id=datasource_id,
                schema_id=schema_ids[table_index % schemas],
                name=f"table_{table_index}",
                object_type="BASE_TABLE",
                status="ACTIVE",
                fingerprint=_FINGERPRINT,
            )
        )
        rows += 1
        for column_index in range(columns_per_table):
            session.add(
                MetadataColumn(
                    id=uuid4(),
                    organization_id=organization_id,
                    table_id=table_id,
                    name=f"column_{column_index}",
                    ordinal_position=column_index + 1,
                    physical_type="character varying(255)",
                    nullable=True,
                    classification="INTERNAL",
                    status="ACTIVE",
                    fingerprint=_FINGERPRINT,
                )
            )
            rows += 1
        session.add(
            MetadataConstraint(
                id=uuid4(),
                organization_id=organization_id,
                datasource_id=datasource_id,
                table_id=table_id,
                name=f"pk_table_{table_index}",
                constraint_type="PRIMARY_KEY",
                columns=["column_0"],
                referenced_table_id=None,
                referenced_columns=[],
                status="ACTIVE",
                fingerprint=_FINGERPRINT,
            )
        )
        rows += 1
        if table_index % 200 == 0:
            await session.flush()
            session.expunge_all()
    await session.commit()
    session.expunge_all()
    return Estate(organization_id=organization_id, datasource_id=datasource_id, rows=rows)


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Measurement:
    label: str
    peak_bytes: int
    seconds: float
    rows: int


async def _measure(label: str, run: Any, *, repeats: int) -> Measurement:
    """Time and memory are measured in *separate* passes, deliberately.

    `tracemalloc` adds per-allocation bookkeeping that is large enough to
    dominate an allocation-heavy loop, and the two paths do not allocate the
    same way -- so timing them while tracing would compare tracing overheads,
    not code. The timed passes run first, with tracing off; the traced pass
    runs afterwards and its wall clock is discarded.
    """
    durations: list[float] = []
    rows = 0
    for _ in range(repeats):
        started = time.perf_counter()
        rows = await run()
        durations.append(time.perf_counter() - started)

    tracemalloc.start()
    tracemalloc.reset_peak()
    await run()
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    return Measurement(
        label=label,
        peak_bytes=peak,
        seconds=statistics.median(durations),
        rows=rows,
    )


async def run_harness(
    *, tables: int, columns_per_table: int, schemas: int, chunk_size: int, repeats: int
) -> list[Measurement]:
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    try:
        async with factory() as session:
            estate = await seed_estate(
                session, tables=tables, columns_per_table=columns_per_table, schemas=schemas
            )
        print(
            f"seeded {estate.rows:,} metadata rows "
            f"({tables:,} tables x {columns_per_table} columns, {schemas} schemas)",
            file=sys.stderr,
        )

        async def before() -> int:
            async with factory() as session:
                projection = await load_projection_before(
                    session, estate.datasource_id, estate.organization_id
                )
                return sum(len(rows) for rows in projection.values())

        async def after() -> int:
            # The projector writes each chunk and drops it; the sink here does
            # the same, so the measurement covers the read path only and is
            # comparable to `before` above, which also only reads.
            total = 0
            async with factory() as session:
                async for chunk in iter_projection_chunks(
                    session,
                    estate.datasource_id,
                    estate.organization_id,
                    tenancy_path={"line_of_business_id": "x"},
                    chunk_size=chunk_size,
                ):
                    total += len(chunk.rows)
            return total

        return [
            await _measure("before (whole-estate load_projection)", before, repeats=repeats),
            await _measure(
                f"after (iter_projection_chunks, chunk={chunk_size})", after, repeats=repeats
            ),
        ]
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tables", type=int, default=2_000)
    parser.add_argument("--columns-per-table", type=int, default=40)
    parser.add_argument("--schemas", type=int, default=20)
    # Defaults to the shipped chunk size so a bare run measures the
    # configuration production actually uses, not a synthetic one.
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_PROJECTION_CHUNK_SIZE)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    measurements = asyncio.run(
        run_harness(
            tables=args.tables,
            columns_per_table=args.columns_per_table,
            schemas=args.schemas,
            chunk_size=args.chunk_size,
            repeats=args.repeats,
        )
    )
    width = max(len(m.label) for m in measurements)
    print(f"{'path'.ljust(width)}  {'rows':>10}  {'peak MiB':>10}  {'seconds':>9}")
    for measurement in measurements:
        print(
            f"{measurement.label.ljust(width)}  {measurement.rows:>10,}  "
            f"{measurement.peak_bytes / 1024 / 1024:>10.1f}  {measurement.seconds:>9.3f}"
        )
    if len(measurements) == 2 and measurements[1].peak_bytes:
        ratio = measurements[0].peak_bytes / measurements[1].peak_bytes
        print(f"\npeak-memory ratio before/after: {ratio:.1f}x")
    if measurements[0].rows != measurements[1].rows:
        print(
            "\nWARNING: the two paths returned different row counts "
            f"({measurements[0].rows} vs {measurements[1].rows}); "
            "the comparison is not like-for-like.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
