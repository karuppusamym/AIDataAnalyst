"""Repair SQL text stored before R11-D16 was fixed (2026-09-15).

Before that date `redact_for_storage` labelled a dollar-quoted routine body -- every
PostgreSQL routine, since the connector stores `pg_get_functiondef` output -- or any
statement sqlglot kept as an opaque `Command`, `PARSED` while its literals survived. A
datasource's next scan rewrites its rows; this repairs rows that will not be rescanned soon.

Only rows whose stored text still holds value-shaped content are touched: that text is
scrubbed lexically and relabelled `LEXICAL` (or, for dbt compiled SQL, dropped and marked
`UNPARSEABLE`, matching what `dbt_artifacts` now stores). Correctly redacted rows are left
exactly as they are, so a run is idempotent. Fingerprint columns are digests of the original
source text and are not changed. Output is counts only -- never stored text.

Dry run by default; `--apply` writes. Run from the repository root with the project's
environment and the platform database configured:

    .venv/Scripts/python.exe scripts/reredact_stored_sql.py
    .venv/Scripts/python.exe scripts/reredact_stored_sql.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter

from sqlalchemy import select

from aida.db import session_factory
from aida.envelope_models import MetadataRoutine, MetadataViewDefinition
from aida.models import DataSource, DbtResource
from aida.sql_redaction import contains_value_shaped_text, scrub_literals_lexically


def repaired_text(stored: str | None, *, dialect: str | None) -> str | None:
    """The value-free replacement for `stored`, or None when it needs no repair."""
    if not stored or not contains_value_shaped_text(stored, dialect=dialect):
        return None
    return scrub_literals_lexically(stored, dialect=dialect)


async def run(*, apply: bool) -> Counter[str]:
    counts: Counter[str] = Counter()
    async with session_factory() as session:
        routines = await session.execute(
            select(MetadataRoutine, DataSource.dialect)
            .join(DataSource, DataSource.id == MetadataRoutine.datasource_id)
            .where(MetadataRoutine.body_sql_redacted.is_not(None))
        )
        for routine, dialect in routines.all():
            counts["metadata_routine.scanned"] += 1
            fixed = repaired_text(routine.body_sql_redacted, dialect=dialect)
            if fixed is None:
                continue
            counts[f"metadata_routine.leaking.{dialect}"] += 1
            if apply:
                routine.body_sql_redacted = fixed
                routine.redaction_status = "LEXICAL"

        views = await session.execute(
            select(MetadataViewDefinition, DataSource.dialect)
            .join(DataSource, DataSource.id == MetadataViewDefinition.datasource_id)
            .where(MetadataViewDefinition.definition_sql_redacted.is_not(None))
        )
        for view, dialect in views.all():
            counts["metadata_view_definition.scanned"] += 1
            fixed = repaired_text(view.definition_sql_redacted, dialect=dialect)
            if fixed is None:
                continue
            counts[f"metadata_view_definition.leaking.{dialect}"] += 1
            if apply:
                view.definition_sql_redacted = fixed
                view.redaction_status = "LEXICAL"

        resources = await session.scalars(
            select(DbtResource).where(DbtResource.compiled_sql_redacted.is_not(None))
        )
        for resource in resources.all():
            counts["dbt_resource.scanned"] += 1
            if repaired_text(resource.compiled_sql_redacted, dialect=None) is None:
                continue
            counts["dbt_resource.leaking"] += 1
            if apply:
                resource.compiled_sql_redacted = None
                resource.sql_parse_status = "UNPARSEABLE"

        if apply:
            await session.commit()
        else:
            await session.rollback()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="write the repair (default: dry run)")
    args = parser.parse_args()
    counts = asyncio.run(run(apply=args.apply))
    mode = "APPLIED" if args.apply else "DRY RUN (nothing written)"
    print(mode)
    for key in sorted(counts):
        print(f"  {key}: {counts[key]}")


if __name__ == "__main__":
    main()
