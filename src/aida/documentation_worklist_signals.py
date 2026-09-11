"""AT-5: gather the documentation worklist's signals from the database.

Moved out of `aida.stewardship_api` on 2026-09-10, unchanged apart from the
entry point's name, so that a second consumer -- the steward agent
(`aida.steward_agent`, ADR-0029) -- ranks tables exactly the way a human
steward is shown them without importing a router.
`documentation_worklist.rank_documentation_worklist` remains the pure ranking;
this module owns every query that feeds it. The endpoint
(`stewardship_api.list_documentation_worklist`) and the agent both call
`gather_documentation_worklist_signals`, so there is one answer to "what should
be documented next", not one per consumer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.catalog_read_model import (
    _business_annotations,
    _description,
    _latest_approved_documentation,
    _latest_pending_drafts,
)
from aida.consumption_lineage import get_consumption_by_resource_counts
from aida.documentation_worklist import TableQuerySignal
from aida.models import DataSource, MetadataSchema, MetadataTable, QueryExecution
from aida.quality_coupling import resolve_table_ids
from aida.stewardship_worklist import enrich_tables

# Mirrors GL-6's own `UNOWNED_BACKLOG_ROUTE_LIMIT` bound: caps both (a) how
# many tables the CX-4 consumption side contributes as ranking candidates,
# and (b) how many additional zero-query-volume tables `include_zero_volume`
# pulls in. The gateway-execution side is bounded separately, by
# `Settings.agent_retrieval_scan_limit` (RT-6's own budget) on *rows scanned*
# per datasource rather than tables returned -- a row-scan bound naturally
# limits the number of distinct tables that can appear from it too.
DOCUMENTATION_WORKLIST_CANDIDATE_LIMIT = 500


async def _query_execution_volume(
    session: AsyncSession,
    *,
    datasources: list[DataSource],
    scan_limit: int,
) -> dict[UUID, tuple[int, datetime]]:
    """How many recent `COMPLETED` `QueryExecution` rows referenced each
    table, aggregated across every datasource in ``datasources``.

    `QueryExecution.referenced_tables` stores SQL-qualified name strings, not
    ids, and a name only resolves unambiguously within one datasource's own
    catalog (two datasources can both have a table named ``customers``), so
    the scan and resolution happen per datasource -- exactly RT-6's own
    `aida.retrieval._table_execution_counts`, reused at the technique level
    (same `aida.quality_coupling.resolve_table_ids` resolver, same
    most-recent-first bounded scan) since AT-5 needs a different aggregate
    (every touched table, not lookup counts for a caller-given set), not a
    fork of RT-6's private, retrieval-scoped helper itself.
    """
    counts: dict[UUID, int] = {}
    last_seen: dict[UUID, datetime] = {}
    for datasource in datasources:
        rows = (
            await session.execute(
                select(QueryExecution.referenced_tables, QueryExecution.created_at)
                .where(
                    QueryExecution.datasource_id == datasource.id,
                    QueryExecution.organization_id == datasource.organization_id,
                    QueryExecution.status == "COMPLETED",
                )
                .order_by(QueryExecution.created_at.desc())
                .limit(scan_limit)
            )
        ).all()
        if not rows:
            continue
        all_names: set[str] = set()
        for referenced_tables, _created_at in rows:
            all_names.update(referenced_tables or [])
        if not all_names:
            continue
        name_to_id = await resolve_table_ids(
            session, datasource=datasource, table_names=sorted(all_names)
        )
        for referenced_tables, created_at in rows:
            # A table referenced twice in one statement counts once for that
            # execution -- this measures how many past *queries* touched the
            # table, the same "queries, not raw name occurrences" rule RT-6
            # applies for the identical reason.
            touched = {
                table_id
                for name in (referenced_tables or [])
                if (table_id := name_to_id.get(name)) is not None
            }
            for table_id in touched:
                counts[table_id] = counts.get(table_id, 0) + 1
                if table_id not in last_seen or created_at > last_seen[table_id]:
                    last_seen[table_id] = created_at
    return {table_id: (count, last_seen[table_id]) for table_id, count in counts.items()}


async def _consumption_volume(
    session: AsyncSession, *, organization_id: UUID, limit: int
) -> dict[UUID, tuple[int, datetime]]:
    """CX-4 consumption-read counts per table, top ``limit`` tables by count.

    `ConsumptionRecord.resource_id` for `resource_type="metadata_table"` is
    already the real `MetadataTable.id` (set by `mcp_server.py`'s
    `record_consumption` call at the point a table is read via MCP), so --
    unlike the gateway-execution side -- no name resolution is needed here.
    """
    rows = await get_consumption_by_resource_counts(
        session,
        organization_id=organization_id,
        resource_type="metadata_table",
        limit=limit,
    )
    result: dict[UUID, tuple[int, datetime]] = {}
    for resource_id, count, last_consumed_at in rows:
        try:
            table_id = UUID(resource_id)
        except ValueError:  # pragma: no cover - defensive, ids are always UUIDs
            continue
        result[table_id] = (count, last_consumed_at)
    return result


async def _documentation_state(
    session: AsyncSession, tables: list[MetadataTable]
) -> dict[UUID, tuple[bool, bool]]:
    """table id -> (is_documented, description_is_proposed), reusing UX-12's
    exact precedence chain (`catalog_read_model._description`) rather than a
    second "is this documented" rule -- see `documentation_worklist.py`'s
    module docstring for why a pending, unapproved draft does not count as
    documented here even though `catalog_read_model` surfaces it as a
    proposal.
    """
    if not tables:
        return {}
    table_ids = [table.id for table in tables]
    documentation = await _latest_approved_documentation(session, table_ids)
    pending_drafts = await _latest_pending_drafts(session, table_ids)
    annotations = await _business_annotations(session, table_ids)
    state: dict[UUID, tuple[bool, bool]] = {}
    for table in tables:
        description, description_is_proposed = _description(
            table,
            documentation=documentation.get(table.id),
            pending_draft=pending_drafts.get(table.id),
            annotation=annotations.get(table.id),
        )
        is_documented = bool(description) and not description_is_proposed
        state[table.id] = (is_documented, description_is_proposed)
    return state


async def gather_documentation_worklist_signals(
    session: AsyncSession,
    *,
    organization_id: UUID,
    scan_limit: int,
    include_zero_volume: bool,
) -> list[TableQuerySignal]:
    """Gather every DB-touching input `rank_documentation_worklist` needs,
    then hand off to that pure function -- this is the only place in AT-5
    that talks to the database.

    The candidate table set is driven by real activity rather than a full
    catalog scan: a table that appears in neither the bounded
    `QueryExecution` scan nor the top-`DOCUMENTATION_WORKLIST_CANDIDATE_LIMIT`
    consumption reads has, by construction, no real query-volume signal to
    rank it by, so it is simply never fetched -- consistent with
    `rank_documentation_worklist`'s own default of excluding zero-volume
    tables, and a lot cheaper than the alternative (composing documentation
    state for an org's entire active-table catalog on every request, which
    `list_catalog_rows`'s own 1M-table docstring notes is exactly the scale
    this platform's catalog surfaces are built not to assume). Only when a
    caller opts into ``include_zero_volume`` does this reach for an
    additional bounded slice of zero-volume active tables.
    """
    datasources = (
        await session.scalars(
            select(DataSource).where(DataSource.organization_id == organization_id)
        )
    ).all()

    execution_volume = await _query_execution_volume(
        session, datasources=list(datasources), scan_limit=scan_limit
    )
    consumption_volume = await _consumption_volume(
        session,
        organization_id=organization_id,
        limit=DOCUMENTATION_WORKLIST_CANDIDATE_LIMIT,
    )
    candidate_ids = set(execution_volume) | set(consumption_volume)

    if include_zero_volume:
        zero_volume_filters: list[Any] = [
            MetadataTable.organization_id == organization_id,
            MetadataTable.status == "ACTIVE",
        ]
        if candidate_ids:
            zero_volume_filters.append(MetadataTable.id.notin_(candidate_ids))
        zero_volume_ids = (
            await session.scalars(
                select(MetadataTable.id)
                .where(*zero_volume_filters)
                .order_by(MetadataTable.id)
                .limit(DOCUMENTATION_WORKLIST_CANDIDATE_LIMIT)
            )
        ).all()
        candidate_ids |= set(zero_volume_ids)

    if not candidate_ids:
        return []

    rows = (
        await session.execute(
            select(MetadataTable, MetadataSchema, DataSource)
            .join(MetadataSchema, MetadataSchema.id == MetadataTable.schema_id)
            .join(DataSource, DataSource.id == MetadataTable.datasource_id)
            .where(
                MetadataTable.organization_id == organization_id,
                MetadataTable.id.in_(candidate_ids),
            )
        )
    ).all()
    candidate_rows = [(table, schema, datasource) for table, schema, datasource in rows]
    documentation_state = await _documentation_state(
        session, [table for table, _, _ in candidate_rows]
    )
    # SW-1 adoption: downstream impact and the five-field deficit, from the
    # same `enrich_tables` `compute_worklist` uses -- so "documented" has one
    # definition on this platform rather than one per surface. AT-5's own
    # UX-12 precedence chain still decides the description field; SW-1 is
    # handed that answer rather than computing a weaker one of its own.
    enrichment = await enrich_tables(
        session,
        organization_id,
        [table.id for table, _, _ in candidate_rows],
        descriptions={
            table.id: documentation_state.get(table.id, (False, False))[0]
            for table, _, _ in candidate_rows
        },
    )

    signals: list[TableQuerySignal] = []
    for table, schema, datasource in candidate_rows:
        exec_count, last_queried_at = execution_volume.get(table.id, (0, None))
        consumption_count, last_consumed_at = consumption_volume.get(table.id, (0, None))
        is_documented, description_is_proposed = documentation_state.get(
            table.id, (False, False)
        )
        deficit = enrichment.get(table.id)
        signals.append(
            TableQuerySignal(
                table_id=table.id,
                table_name=table.name,
                schema_name=schema.name,
                datasource_name=datasource.name,
                query_execution_count=exec_count,
                consumption_read_count=consumption_count,
                last_queried_at=last_queried_at,
                last_consumed_at=last_consumed_at,
                is_documented=is_documented,
                description_is_proposed=description_is_proposed,
                downstream_count=deficit.downstream_count if deficit else 0,
                missing=deficit.missing if deficit else (),
            )
        )
    return signals
