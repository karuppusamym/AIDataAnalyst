"""Resolve the table names a SQL lineage parser extracted to catalog table ids.

Moved out of `view_lineage_api._resolve_table_ids` on 2026-09-10, unchanged, so
that the lineage agent (`aida.lineage_agent`, ADR-0029) resolves a parsed
view's sources exactly the way a person's parse does, without importing a
router. `procedure_lineage_api` still carries its own identical copy.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import MetadataCatalog, MetadataSchema, MetadataTable


async def resolve_lineage_table_ids(
    session: AsyncSession, datasource_id: UUID, table_names: set[str]
) -> dict[str, UUID]:
    """Resolve raw table-name strings the parser extracted to `MetadataTable.id`.

    AT-D2: `source_table_id`/`target_table_id` were never populated, so a
    parsed edge could never be traversed even once the unified lineage graph
    (LN-7/AT-10) was ready to fold it in -- `_build_unified_graph` already
    filters both columns to non-NULL and simply got nothing.

    Matched case-insensitively against every active table's fully-qualified
    (`catalog.schema.table`), schema-qualified (`schema.table`), and bare
    name -- the parser's own resolution may return any of those three forms
    depending on how the SQL qualified the reference. On a same-name
    collision across schemas the first table loaded wins; that ambiguity is
    inherent to a free-text name with no schema context, not something this
    lookup can resolve on its own.
    """
    if not table_names:
        return {}
    rows = (
        await session.execute(
            select(MetadataTable.id, MetadataTable.name, MetadataSchema.name, MetadataCatalog.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(MetadataCatalog, MetadataCatalog.id == MetadataSchema.catalog_id)
            .where(
                MetadataTable.datasource_id == datasource_id,
                MetadataTable.status == "ACTIVE",
            )
        )
    ).all()
    by_key: dict[str, UUID] = {}
    for table_id, table_name, schema_name, catalog_name in rows:
        for key in (
            f"{catalog_name}.{schema_name}.{table_name}",
            f"{schema_name}.{table_name}",
            table_name,
        ):
            by_key.setdefault(key.lower(), table_id)
    return {name: by_key[name.lower()] for name in table_names if name.lower() in by_key}
