"""AT-5: query-history-ranked documentation worklist for stewards.

Stewards need a *prioritized* list of undocumented/under-described tables --
not the full undocumented-table set (`aida.api.list_catalog_rows` already
answers that, filterable but unranked), and not a retrieval-time signal like
RT-6's `usage_popularity` (`aida.retrieval._table_execution_counts`), which
exists to bias which tables an *agent* considers next, not which tables a
*human steward* should document next. Two different consumers, two different
shapes -- see `Docs/60-delivery/03-tracker.md` AT-5's own note.

Real query-volume sources
--------------------------
``query_execution_count``
    `QueryExecution.referenced_tables` (governed SQL execution,
    `aida.query_gateway`) -- one row per executed statement, carrying the
    table names it touched and a `created_at`. Resolved to real
    `MetadataTable` ids the same way RT-6 does
    (`aida.quality_coupling.resolve_table_ids`), per datasource (name
    resolution is only unambiguous within one datasource's catalog).
``consumption_read_count``
    `ConsumptionRecord` rows with `resource_type="metadata_table"` (CX-4,
    MCP/context-product reads, `aida.consumption_lineage`) -- `resource_id`
    is already the real `MetadataTable.id`, recorded with `consumed_at`.

Both retain per-table identity and recency, which is what makes ranking
meaningful; see `stewardship_api._documentation_worklist_signals` for how
they are gathered (DB-touching, bounded like RT-6's own scan) and fed into
the pure function below.

Documentation determination
-----------------------------
Reused verbatim from UX-12's precedence chain
(`aida.catalog_read_model._description`): a table counts as documented only
when it has a real, non-proposed description (an approved GL-9 readme, an
approved business annotation, or the connector-sourced comment) -- a table
with only a `PENDING_APPROVAL` draft (`description_is_proposed=True`) is
still "under-described" for this worklist's purposes, since nothing has
actually been approved yet. No second notion of "documented" is invented
here.

Design choices (mirrors TL-6/CN-7's "every factor inspectable" convention --
`aida.connector_health`, `aida.tool_first_rate`): pure, DB-free, every input
factor that drove the order is on the response, not just the final rank.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class TableQuerySignal:
    """One table's real usage/documentation signal, gathered by the caller.

    Deliberately DB-free: everything here is a plain value, so
    `rank_documentation_worklist` can be tested without a database and the
    caller (`stewardship_api.py`) owns every query.
    """

    table_id: UUID
    table_name: str
    schema_name: str
    datasource_name: str
    query_execution_count: int
    consumption_read_count: int
    last_queried_at: datetime | None
    last_consumed_at: datetime | None
    is_documented: bool
    description_is_proposed: bool


@dataclass(frozen=True, slots=True)
class DocumentationWorklistEntry:
    """One ranked row. `rank` and `query_volume` are derived, not inputs --
    kept here (rather than computed twice) so the response and the ordering
    it reflects can never disagree.
    """

    table_id: UUID
    table_name: str
    schema_name: str
    datasource_name: str
    rank: int
    query_execution_count: int
    consumption_read_count: int
    query_volume: int
    last_queried_at: datetime | None
    last_consumed_at: datetime | None
    description_is_proposed: bool


def rank_documentation_worklist(
    signals: list[TableQuerySignal],
    *,
    limit: int,
    offset: int = 0,
    include_zero_volume: bool = False,
) -> tuple[list[DocumentationWorklistEntry], int]:
    """Rank undocumented/under-described tables by real query volume.

    Design choices, stated rather than left implicit:

    - **Documented tables are excluded entirely** (``is_documented`` filters
      the input set before ranking), not merely demoted -- this is meant to
      *be* the worklist, mirroring GL-6's bounded backlog shape, not a
      general catalog view with a documentation column.
    - **Ties break deterministically**: by table name, then table id -- two
      tables with equal volume never reorder between two calls with the
      same input, which matters for stable pagination.
    - **Zero-query-volume tables are excluded by default**
      (``include_zero_volume=False``). The whole point of this worklist is
      "ranked by real query volume" (tracker AT-5): a table nobody has
      queried or read has no real signal to rank it *by* -- ranking it
      arbitrarily "last" would dress up a guess as a measurement, and the
      unranked full undocumented-table set is already available elsewhere
      (`GET /organizations/{id}/catalog/rows`, unranked, filterable). A
      caller that still wants them can opt in with
      ``include_zero_volume=True``; because the sort key is volume
      descending, opted-in zero-volume rows sort after every real-volume row
      automatically, so "included" and "ranked last" are the same outcome,
      not a special case.

    Returns ``(page, total_candidates)`` -- ``total_candidates`` is the count
    of the full ranked (documented tables already excluded, zero-volume
    included or not per ``include_zero_volume``) candidate set, independent
    of ``limit``/``offset``, the same "total across the whole filtered set"
    contract `Page.total` carries elsewhere in this codebase.
    """
    candidates = [signal for signal in signals if not signal.is_documented]
    if not include_zero_volume:
        candidates = [
            signal
            for signal in candidates
            if signal.query_execution_count + signal.consumption_read_count > 0
        ]

    ordered = sorted(
        candidates,
        key=lambda signal: (
            -(signal.query_execution_count + signal.consumption_read_count),
            signal.table_name.lower(),
            str(signal.table_id),
        ),
    )
    total = len(ordered)
    page = ordered[offset : offset + limit]
    entries = [
        DocumentationWorklistEntry(
            table_id=signal.table_id,
            table_name=signal.table_name,
            schema_name=signal.schema_name,
            datasource_name=signal.datasource_name,
            rank=offset + index + 1,
            query_execution_count=signal.query_execution_count,
            consumption_read_count=signal.consumption_read_count,
            query_volume=signal.query_execution_count + signal.consumption_read_count,
            last_queried_at=signal.last_queried_at,
            last_consumed_at=signal.last_consumed_at,
            description_is_proposed=signal.description_is_proposed,
        )
        for index, signal in enumerate(page)
    ]
    return entries, total
