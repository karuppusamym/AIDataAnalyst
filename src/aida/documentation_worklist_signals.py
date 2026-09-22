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

R11-FP08 adds `gather_routine_documentation_worklist_signals` beside it, on the
same division of labour: this module owns every query, the pure
`documentation_worklist.rank_routine_documentation_worklist` owns the order. It
spends no new scan budget -- it reuses the two table-volume aggregates above and
turns them into a routine's ranking through the procedure lineage the platform
already holds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.catalog_read_model import (
    _business_annotations,
    _description,
    _latest_approved_documentation,
    _latest_pending_drafts,
    _withdrawn_documentation_table_ids,
)
from aida.consumption_lineage import get_consumption_by_resource_counts
from aida.documentation_worklist import RoutineDocumentationSignal, TableQuerySignal
from aida.envelope_models import (
    MetadataRoutine,
    RoutineDescriptionDraft,
    RoutineDocumentation,
    RoutineDocumentationVersion,
)
from aida.models import DataSource, MetadataSchema, MetadataTable, QueryExecution
from aida.procedure_lineage_models import DeepProcedureLineageEdge
from aida.quality_coupling import resolve_table_ids
from aida.stewardship_worklist import enrich_tables

#: The open-draft states `uq_routine_description_draft_open` allows exactly one
#: of per routine. Named here rather than inlined so the worklist's notion of
#: "a draft is already in flight" is the index's, not a second list.
_OPEN_ROUTINE_DRAFT_STATUSES = ("DRAFT", "PENDING_APPROVAL")

#: Refused by name wherever routines are consumed in this codebase; see
#: `documentation_worklist.PACKAGE_NOT_RANKABLE`.
_PACKAGE = "PACKAGE"

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
    # A table whose description was withdrawn has to come back onto the
    # worklist as undocumented -- that is the point of retiring it. Reading it
    # as documented off the business annotation would hide the one asset a
    # steward just said needs re-describing.
    withdrawn = await _withdrawn_documentation_table_ids(session, table_ids)
    state: dict[UUID, tuple[bool, bool]] = {}
    for table in tables:
        description, description_is_proposed = _description(
            table,
            documentation=documentation.get(table.id),
            pending_draft=pending_drafts.get(table.id),
            annotation=annotations.get(table.id),
            documentation_withdrawn=table.id in withdrawn,
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
    # shared `enrich_tables` scorer, so "documented" has one definition on
    # this platform rather than one per surface. AT-5's own
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


# ---------------------------------------------------------------------------
# R11-FP08: the same gather, for routines
# ---------------------------------------------------------------------------


async def _routine_lineage_reach(
    session: AsyncSession, *, organization_id: UUID, routine_ids: list[UUID]
) -> tuple[dict[UUID, set[UUID]], dict[UUID, set[UUID]]]:
    """`(writes, reads)` table-id sets per routine, from ACTIVE lineage only.

    The rule `retrieval.hybrid_retrieve` applies to the same edges, for the same
    reason: an agent's PROPOSED edge is a proposal nobody has decided, so it must
    not steer what a steward is told to document next any more than it steers an
    answer. `is_intermediate` edges name a temp table, which is not a thing to
    describe.
    """
    writes: dict[UUID, set[UUID]] = {}
    reads: dict[UUID, set[UUID]] = {}
    if not routine_ids:
        return writes, reads
    rows = await session.execute(
        select(
            DeepProcedureLineageEdge.routine_id,
            DeepProcedureLineageEdge.source_table_id,
            DeepProcedureLineageEdge.target_table_id,
            DeepProcedureLineageEdge.is_write,
        ).where(
            DeepProcedureLineageEdge.organization_id == organization_id,
            DeepProcedureLineageEdge.routine_id.in_(routine_ids),
            DeepProcedureLineageEdge.review_status == "ACTIVE",
            DeepProcedureLineageEdge.is_intermediate.is_(False),
        )
    )
    for routine_id, source_table_id, target_table_id, is_write in rows.all():
        if source_table_id is not None:
            reads.setdefault(routine_id, set()).add(source_table_id)
        if target_table_id is not None and is_write:
            writes.setdefault(routine_id, set()).add(target_table_id)
    return writes, reads


async def _routine_documentation_state(
    session: AsyncSession, *, organization_id: UUID, routine_ids: list[UUID]
) -> dict[UUID, tuple[bool, bool]]:
    """routine id -> (is_documented, description_is_proposed).

    "Documented" is one APPROVED `RoutineDocumentationVersion` and nothing else --
    `routine_description_service.current_routine_descriptions`' own rule, restated
    here with the `organization_id`/`datasource_id` every worklist read carries
    (INV-5) rather than re-derived into a second definition. Two consequences worth
    stating: a `WITHDRAWN` version is not `APPROVED`, so retiring a description puts
    the routine back on the worklist, which is the point of retiring it; and a
    `SUPERSEDED` one is not `APPROVED` either, but its replacement is, so a
    re-described routine stays off.

    `description_is_proposed` is an open `RoutineDescriptionDraft` --
    `_OPEN_ROUTINE_DRAFT_STATUSES`, the two states
    `uq_routine_description_draft_open` allows exactly one of.
    """
    if not routine_ids:
        return {}
    described = set(
        (
            await session.scalars(
                select(RoutineDocumentation.routine_id)
                .join(
                    RoutineDocumentationVersion,
                    RoutineDocumentationVersion.documentation_id == RoutineDocumentation.id,
                )
                .where(
                    RoutineDocumentation.routine_id.in_(routine_ids),
                    RoutineDocumentation.organization_id == organization_id,
                    RoutineDocumentationVersion.organization_id == organization_id,
                    RoutineDocumentationVersion.status == "APPROVED",
                )
            )
        ).all()
    )
    proposed = set(
        (
            await session.scalars(
                select(RoutineDescriptionDraft.routine_id).where(
                    RoutineDescriptionDraft.routine_id.in_(routine_ids),
                    RoutineDescriptionDraft.organization_id == organization_id,
                    RoutineDescriptionDraft.status.in_(_OPEN_ROUTINE_DRAFT_STATUSES),
                )
            )
        ).all()
    )
    return {
        routine_id: (routine_id in described, routine_id in proposed)
        for routine_id in routine_ids
    }


async def gather_routine_documentation_worklist_signals(
    session: AsyncSession,
    *,
    organization_id: UUID,
    scan_limit: int,
    include_zero_volume: bool,
) -> list[RoutineDocumentationSignal]:
    """Gather every DB-touching input `rank_routine_documentation_worklist` needs.

    The candidate set is driven by real activity for the reason the table gather's
    is, but the activity is a different one: a routine has no traffic of its own, so
    the driver is "an ACTIVE, non-intermediate edge says this routine writes a table
    that real queries or real MCP reads have touched". The table-volume maps are the
    *same two* the table worklist spends its scan budget on
    (`_query_execution_volume`, `_consumption_volume`), so nothing new is measured
    here -- the lineage the platform already holds is what turns a table's traffic
    into a routine's ranking.

    `PACKAGE` is excluded in SQL: this is a discovery surface, so there are no
    caller-supplied ids to refuse, and every other routine-consuming discovery path
    in this codebase (`footprint_gaps`, `footprint_gap_detail`) excludes it the same
    way. A package that reaches the pure ranker anyway is refused by name there
    (`documentation_worklist.RoutineNotRankable`), so the exclusion is provable
    rather than a silent skip.
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
    volume_by_table: dict[UUID, int] = {}
    for table_id in set(execution_volume) | set(consumption_volume):
        volume_by_table[table_id] = (
            execution_volume.get(table_id, (0, None))[0]
            + consumption_volume.get(table_id, (0, None))[0]
        )

    candidate_ids: set[UUID] = set()
    if volume_by_table:
        candidate_ids = set(
            (
                await session.scalars(
                    select(DeepProcedureLineageEdge.routine_id)
                    .where(
                        DeepProcedureLineageEdge.organization_id == organization_id,
                        DeepProcedureLineageEdge.review_status == "ACTIVE",
                        DeepProcedureLineageEdge.is_intermediate.is_(False),
                        DeepProcedureLineageEdge.is_write.is_(True),
                        DeepProcedureLineageEdge.target_table_id.in_(
                            sorted(volume_by_table)
                        ),
                    )
                    .distinct()
                    .limit(DOCUMENTATION_WORKLIST_CANDIDATE_LIMIT)
                )
            ).all()
        )

    if include_zero_volume:
        zero_volume_filters: list[Any] = [
            MetadataRoutine.organization_id == organization_id,
            MetadataRoutine.status == "ACTIVE",
            func.upper(MetadataRoutine.routine_type) != _PACKAGE,
        ]
        if candidate_ids:
            zero_volume_filters.append(MetadataRoutine.id.notin_(candidate_ids))
        candidate_ids |= set(
            (
                await session.scalars(
                    select(MetadataRoutine.id)
                    .where(*zero_volume_filters)
                    .order_by(MetadataRoutine.id)
                    .limit(DOCUMENTATION_WORKLIST_CANDIDATE_LIMIT)
                )
            ).all()
        )

    if not candidate_ids:
        return []

    rows = (
        await session.execute(
            select(MetadataRoutine, MetadataSchema.name, DataSource.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .join(DataSource, DataSource.id == MetadataRoutine.datasource_id)
            .where(
                MetadataRoutine.organization_id == organization_id,
                MetadataRoutine.id.in_(sorted(candidate_ids)),
                MetadataRoutine.status == "ACTIVE",
                func.upper(MetadataRoutine.routine_type) != _PACKAGE,
            )
        )
    ).all()
    routine_ids = [routine.id for routine, _schema_name, _datasource_name in rows]
    writes, reads = await _routine_lineage_reach(
        session, organization_id=organization_id, routine_ids=routine_ids
    )
    documentation_state = await _routine_documentation_state(
        session, organization_id=organization_id, routine_ids=routine_ids
    )

    signals: list[RoutineDocumentationSignal] = []
    for routine, schema_name, datasource_name in rows:
        written = writes.get(routine.id, set())
        is_documented, description_is_proposed = documentation_state.get(
            routine.id, (False, False)
        )
        signals.append(
            RoutineDocumentationSignal(
                routine_id=routine.id,
                routine_name=routine.name,
                schema_name=schema_name,
                datasource_name=datasource_name,
                routine_type=routine.routine_type,
                # Borrowed from the whole volume map, not the table worklist's
                # candidate set: a *documented* hot table still makes whatever
                # writes it worth describing.
                written_table_query_volume=sum(
                    volume_by_table.get(table_id, 0) for table_id in written
                ),
                writes_table_count=len(written),
                reads_table_count=len(reads.get(routine.id, set())),
                is_documented=is_documented,
                description_is_proposed=description_is_proposed,
            )
        )
    return signals
