#!/usr/bin/env python3
"""CT-2 scale harness — bulk-populate Aida's own catalog DB with synthetic tables/columns.

Writes real rows through the real ORM models (`aida.models`, which re-exports
`atlas.modules.catalog.models` / `atlas.modules.connectivity.models` /
`atlas.modules.identity_tenancy.models` per the ST-05 module split — see
`src/atlas/modules/catalog/models.py`'s own docstring) so the data this script
produces is byte-for-byte indistinguishable, at the schema level, from a real
`connector.discover()` snapshot persisted by `persist_discovery_snapshot`
(`src/aida/workflows/activities.py`). It does NOT go through that persistence
path itself — there is no live datasource to discover from — it inserts
catalog rows directly, in bulk, which is the only way to reach 100K tables in
a reasonable time on a laptop.

Everything this script creates lives under one synthetic Organization whose
slug defaults to `scale-harness-ct2` (see README.md in this directory for the
full rationale and the honesty caveat about proxy vs. literal tracker scale).
`ct2_cleanup.py` deletes that organization and everything FK-chained under it.

Bulk-insert strategy: SQLAlchemy Core `insert(Table)` executed with a list of
per-row parameter dicts (`session.execute(stmt, rows)`), batched at
`--batch-size` rows per call. This is the "executemany in batches" shape the
harness plan calls for — asyncpg's SQLAlchemy dialect sends each batch as one
multi-row round trip, not one INSERT per row — and it goes through the real
`Table` objects (`MetadataTable.__table__` etc.), so every column, default,
FK and NOT NULL constraint is exactly what the live schema enforces. Column
default values that ARE Python-side per-row defaults on the model
(`TimestampMixin.created_at`/`updated_at`, `MetadataTable.status`, ...) are
honored by Core `insert()` the same way they would be through the ORM, since
they are defined on the `Column`/`mapped_column` itself, not on the class.

Run this after starting the normal dev stack's `postgres` service (see
README.md Phase A). Requires `AIDA_ENVIRONMENT` to be set in the shell (any
value; `development` matches the rest of the dev workflow) — same requirement
`atlas.platform.config.Settings` imposes on every other script/test that
constructs it outside pytest.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
import time
from uuid import uuid4

from sqlalchemy import insert, select

from aida.models import (
    DataDomain,
    DataSource,
    LineOfBusiness,
    MetadataCatalog,
    MetadataColumn,
    MetadataSchema,
    MetadataTable,
    Organization,
    Project,
)
from atlas.platform.db import get_session_factory

DEFAULT_ORG_SLUG = "scale-harness-ct2"
DEFAULT_DATASOURCE_NAME = "scale-harness-ct2-datasource"

_PHYSICAL_TYPES = (
    "integer",
    "bigint",
    "varchar(255)",
    "text",
    "boolean",
    "timestamp with time zone",
    "numeric(18,2)",
    "date",
    "uuid",
)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--tables",
        type=int,
        default=100_000,
        help="number of MetadataTable rows to create (default: 100000, the "
        "harness's deliberate 10%% proxy for the tracker's literal 1M target — "
        "see README.md). Lower this if the run is too slow on your machine.",
    )
    parser.add_argument(
        "--min-columns",
        type=int,
        default=15,
        help="minimum columns per table (default: 15)",
    )
    parser.add_argument(
        "--max-columns",
        type=int,
        default=35,
        help="maximum columns per table (default: 35; min/max default to an "
        "average of ~25 columns/table, matching the harness's ~2.5M-column "
        "target at 100000 tables)",
    )
    parser.add_argument(
        "--table-batch-size",
        type=int,
        default=2_000,
        help="MetadataTable rows per bulk-insert batch (default: 2000)",
    )
    parser.add_argument(
        "--column-batch-size",
        type=int,
        default=10_000,
        help="MetadataColumn rows per bulk-insert batch (default: 10000)",
    )
    parser.add_argument("--org-slug", default=DEFAULT_ORG_SLUG)
    parser.add_argument("--datasource-name", default=DEFAULT_DATASOURCE_NAME)
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="RNG seed for per-table column counts/types — deterministic across runs",
    )
    return parser.parse_args(argv)


async def _ensure_scope(
    session, *, org_slug: str, datasource_name: str
) -> tuple[Organization, DataSource, MetadataSchema]:
    """Create (or refuse to re-create) the synthetic org/lob/domain/project/
    datasource/catalog/schema hierarchy this harness's tables hang off of.

    Refuses outright if an organization with this slug already exists — this
    script is meant to run once against a clean synthetic scope; re-running it
    against a scope that already has tables would silently double the table
    count and skew every timing number. Run `ct2_cleanup.py` first to start
    over.
    """
    existing = await session.scalar(select(Organization).where(Organization.slug == org_slug))
    if existing is not None:
        raise SystemExit(
            f"organization slug {org_slug!r} already exists (id={existing.id}). "
            f"Run `ct2_cleanup.py --org-slug {org_slug}` first, or pass a "
            f"different --org-slug."
        )

    org = Organization(id=uuid4(), name="CT-2 Scale Harness", slug=org_slug)
    lob = LineOfBusiness(
        id=uuid4(), organization_id=org.id, name="Scale Harness", code="SCALEHARNESS"
    )
    domain = DataDomain(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        name="Scale Harness Domain",
        code="SCALEHARNESS",
        is_default=True,
    )
    project = Project(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        name="Scale Harness Project",
        slug="scale-harness-ct2-project",
    )
    datasource = DataSource(
        id=uuid4(),
        organization_id=org.id,
        line_of_business_id=lob.id,
        data_domain_id=domain.id,
        project_id=project.id,
        name=datasource_name,
        connector_type="postgres",
        dialect="postgres",
        environment="DEV",
        network_zone="default",
        # Never resolved: this datasource exists only to give CT-2's tables a
        # realistic organization_id/datasource_id scope, no discovery ever
        # runs against it. A syntactically valid but unresolvable reference
        # documents that rather than pointing at a real secret.
        credential_reference="env://AIDA_SCALE_HARNESS_CT2_UNUSED",
    )
    catalog = MetadataCatalog(
        id=uuid4(),
        organization_id=org.id,
        datasource_id=datasource.id,
        name="scale_harness_catalog",
        fingerprint="ct2-scale-harness",
    )
    # These ORM objects are linked by UUID values rather than SQLAlchemy
    # relationship attributes, so preserve their foreign-key order explicitly
    # instead of relying on the unit-of-work's mapper ordering.
    session.add(org)
    await session.flush()
    session.add(lob)
    await session.flush()
    session.add(domain)
    await session.flush()
    session.add(project)
    await session.flush()
    session.add(datasource)
    await session.flush()
    session.add(catalog)
    await session.flush()
    schema = MetadataSchema(
        id=uuid4(),
        organization_id=org.id,
        catalog_id=catalog.id,
        name="public",
        fingerprint="ct2-scale-harness",
    )
    session.add(schema)
    await session.commit()
    return org, datasource, schema


def _table_row(
    *, organization_id, datasource_id, schema_id, index: int, width: int
) -> dict:
    return {
        "id": uuid4(),
        "organization_id": organization_id,
        "datasource_id": datasource_id,
        "schema_id": schema_id,
        # Zero-padded so lexical order == numeric order (avoids "table_10" <
        # "table_2" as plain strings) — mirrors tests/test_catalog_pagination.py.
        "name": f"table_{index:0{width}d}",
        "object_type": "BASE_TABLE",
        "status": "ACTIVE",
        "fingerprint": f"ct2-fp-{index}",
        "source_description": None,
    }


def _column_rows(
    *, organization_id, table_id, count: int, rng: random.Random
) -> list[dict]:
    rows = []
    for ordinal in range(1, count + 1):
        physical_type = rng.choice(_PHYSICAL_TYPES)
        rows.append(
            {
                "id": uuid4(),
                "organization_id": organization_id,
                "table_id": table_id,
                "name": f"col_{ordinal:03d}",
                "ordinal_position": ordinal,
                "physical_type": physical_type,
                "nullable": ordinal != 1,  # first column plays the "id"/PK-ish column
                "default_expression": None,
                "source_description": None,
                "classification": "UNCLASSIFIED",
                "classification_source": "RULE",
                "status": "ACTIVE",
                "fingerprint": f"ct2-col-fp-{table_id}-{ordinal}",
            }
        )
    return rows


async def _run(args: argparse.Namespace) -> None:
    if args.min_columns > args.max_columns:
        raise SystemExit("--min-columns must be <= --max-columns")

    session_factory = get_session_factory()
    async with session_factory() as session:
        org, datasource, schema = await _ensure_scope(
            session, org_slug=args.org_slug, datasource_name=args.datasource_name
        )

    print(
        f"scope ready: organization={org.id} (slug={args.org_slug}) "
        f"datasource={datasource.id} schema={schema.id}"
    )

    rng = random.Random(args.seed)  # noqa: S311 -- deterministic synthetic fixture data
    width = len(str(args.tables))
    total_columns = 0
    started = time.monotonic()

    async with session_factory() as session:
        table_table = MetadataTable.__table__
        column_table = MetadataColumn.__table__
        for batch_start in range(0, args.tables, args.table_batch_size):
            batch_end = min(batch_start + args.table_batch_size, args.tables)
            table_rows = [
                _table_row(
                    organization_id=org.id,
                    datasource_id=datasource.id,
                    schema_id=schema.id,
                    index=i,
                    width=width,
                )
                for i in range(batch_start, batch_end)
            ]
            await session.execute(insert(table_table), table_rows)

            column_rows: list[dict] = []
            for row in table_rows:
                count = rng.randint(args.min_columns, args.max_columns)
                column_rows.extend(
                    _column_rows(
                        organization_id=org.id,
                        table_id=row["id"],
                        count=count,
                        rng=rng,
                    )
                )
            total_columns += len(column_rows)
            for col_start in range(0, len(column_rows), args.column_batch_size):
                chunk = column_rows[col_start : col_start + args.column_batch_size]
                await session.execute(insert(column_table), chunk)

            await session.commit()
            elapsed = time.monotonic() - started
            rate = batch_end / elapsed if elapsed > 0 else 0.0
            print(
                f"  {batch_end:>7}/{args.tables} tables "
                f"({total_columns:>9} columns so far) "
                f"— {elapsed:6.1f}s elapsed, {rate:6.0f} tables/s",
                flush=True,
            )

    elapsed = time.monotonic() - started
    print(
        f"done: {args.tables} tables, {total_columns} columns in {elapsed:.1f}s "
        f"under organization {org.id} (slug={args.org_slug})"
    )
    print(
        "Next: run ct2_measure_pagination.py against this same --org-slug to "
        "measure list_tables/list_columns latency by page depth."
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
