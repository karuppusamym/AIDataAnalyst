#!/usr/bin/env python3
"""CT-2 scale harness — delete the synthetic organization `ct2_generate_catalog.py`
created, and everything FK-chained under it.

Deletes bottom-up (columns, tables, schema, catalog, datasource, project,
domain, line-of-business, organization) inside one transaction per stage so a
crash mid-cleanup leaves a smaller, still-consistent scope to retry rather
than an orphaned mix. Every one of these tables' `organization_id` (or, for
`MetadataColumn`/`MetadataTable`, `organization_id` directly) makes this a
plain `DELETE ... WHERE organization_id = :org_id` per table — no need to
walk the FK graph row by row.

Only ever touches rows scoped to the given `--org-slug` (default
`scale-harness-ct2`, matching the generator's default) — never a bulk
delete over the whole catalog.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import delete, select

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


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--org-slug", default=DEFAULT_ORG_SLUG)
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> None:
    session_factory = get_session_factory()
    async with session_factory() as session:
        org = await session.scalar(select(Organization).where(Organization.slug == args.org_slug))
        if org is None:
            print(f"no organization with slug {args.org_slug!r} — nothing to clean up")
            return
        org_id = org.id
        print(f"deleting organization {org_id} (slug={args.org_slug}) and everything under it...")

        for label, stmt in [
            ("columns", delete(MetadataColumn).where(MetadataColumn.organization_id == org_id)),
            ("tables", delete(MetadataTable).where(MetadataTable.organization_id == org_id)),
            ("schemas", delete(MetadataSchema).where(MetadataSchema.organization_id == org_id)),
            ("catalogs", delete(MetadataCatalog).where(MetadataCatalog.organization_id == org_id)),
            ("datasources", delete(DataSource).where(DataSource.organization_id == org_id)),
            ("projects", delete(Project).where(Project.organization_id == org_id)),
            ("data_domains", delete(DataDomain).where(DataDomain.organization_id == org_id)),
            (
                "lines_of_business",
                delete(LineOfBusiness).where(LineOfBusiness.organization_id == org_id),
            ),
            ("organization", delete(Organization).where(Organization.id == org_id)),
        ]:
            result = await session.execute(stmt)
            await session.commit()
            print(f"  {label}: {result.rowcount} row(s) deleted")

    print("cleanup complete")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
