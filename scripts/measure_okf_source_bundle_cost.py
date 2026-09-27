#!/usr/bin/env python3
"""R11-OKF02: what the first open of a product-less object costs on a large source.

The Catalog's object read builds and publishes the object's whole source bundle the first time
anyone opens an object no product holds and no stored bundle exists (`read_object_source_
knowledge`). This measures that first open, and the second (served from the store), on a
synthetic estate of N tables with C columns each, against the database `--database-url` names.

Point it at a throwaway database -- it creates the schema with `create_all` and fills it
from the OKF test estate, which leans on SQLite not enforcing foreign keys, so use SQLite:

    AIDA_ENVIRONMENT=development .venv/Scripts/python.exe \
        scripts/measure_okf_source_bundle_cost.py \
        --database-url sqlite+aiosqlite:///okf-cost.db --tables 200 1000 2000 --columns 12

Nothing here calls a model or a source database. Each size runs in a fresh organization.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

import aida.models  # noqa: E402,F401 -- registers every table
from aida.config import Settings  # noqa: E402
from aida.db import Base  # noqa: E402
from aida.models import MetadataColumn, MetadataTable  # noqa: E402
from aida.okf_store import read_object_source_knowledge  # noqa: E402
from tests.test_okf_export import _context, _estate  # noqa: E402


async def _grow(
    session: AsyncSession, estate: dict[str, object], tables: int, columns: int
) -> None:
    datasource, _catalog, schema = estate["datasources"]["people"]  # type: ignore[index]
    org_id = estate["organization"].id  # type: ignore[attr-defined]
    batch_tables: list[MetadataTable] = []
    batch_columns: list[MetadataColumn] = []
    for index in range(tables):
        table = MetadataTable(
            id=uuid4(),
            organization_id=org_id,
            datasource_id=datasource.id,
            schema_id=schema.id,
            name=f"t_{index:05d}",
            object_type="BASE_TABLE",
            status="ACTIVE",
            fingerprint="f",
        )
        batch_tables.append(table)
        for ordinal in range(1, columns + 1):
            batch_columns.append(
                MetadataColumn(
                    id=uuid4(),
                    organization_id=org_id,
                    table_id=table.id,
                    name=f"c_{ordinal:03d}",
                    ordinal_position=ordinal,
                    physical_type="varchar(40)",
                    nullable=True,
                    classification="INTERNAL",
                    status="ACTIVE",
                    fingerprint="f",
                )
            )
        if len(batch_columns) >= 20_000:
            session.add_all(batch_tables)
            await session.flush()
            session.add_all(batch_columns)
            await session.flush()
            batch_tables, batch_columns = [], []
    session.add_all(batch_tables)
    await session.flush()
    session.add_all(batch_columns)
    await session.commit()


async def _measure(url: str, sizes: list[int], columns: int) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    settings = Settings(_env_file=None)
    print(f"database: {engine.dialect.name}; columns per table: {columns}")
    for tables in sizes:
        async with maker() as session:
            session.info["maker"] = maker
            estate = await _estate(session)
            await _grow(session, estate, tables, columns)
            context = _context(estate["organization"].id)
            target = estate["tables"]["people.salaries"]
            timings = []
            for _ in range(2):
                started = time.perf_counter()
                found = await read_object_source_knowledge(session, target.id, context, settings)
                await session.commit()
                timings.append(time.perf_counter() - started)
            stored = found.stored
            documents = stored.publication.document_count if stored is not None else None
            print(
                f"tables={tables:>6} columns={tables * columns:>7} state={found.state} "
                f"documents={documents} first_open={timings[0]:.2f}s "
                f"second_open={timings[1]:.3f}s"
            )
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--tables", type=int, nargs="+", default=[200, 1000])
    parser.add_argument("--columns", type=int, default=12)
    args = parser.parse_args()
    asyncio.run(_measure(args.database_url, args.tables, args.columns))


if __name__ == "__main__":
    main()
