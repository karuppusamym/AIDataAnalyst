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
meaningful; see `documentation_worklist_signals.gather_documentation_worklist_signals` for how
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

Routines (R11-FP08)
-------------------
R11-FP08 gave a routine a governed description and left the worklist unable to
ask for one: everything above is `table_id`-keyed, so a procedure nobody has
described -- exactly the gap this module exists to rank -- could not appear.
`rank_routine_documentation_worklist` below adds that, as a *second ranked view
of one surface* rather than a second surface: same scorer
(`stewardship_worklist.score_item`), same exclusion rule, same
offset/limit/total contract, same "every factor on the row" convention.

**A routine's ranking terms are not the table's.** A table's usage is its own
traffic; nobody queries a stored procedure, so its own traffic is always zero
and a table-shaped ranking would score every routine identically at nothing.
What makes a routine worth describing is what it *produces*: a procedure that
writes three tables many questions read is high-value to describe even though
the procedure itself is never queried. So:

* **usage** is borrowed, not measured -- the combined query/consumption volume
  of the tables the routine *writes*, over ACTIVE, non-intermediate
  procedure-lineage edges. Writes rather than reads, because a table's contents
  are produced by what writes it: a reader of a hot table is a consumer of
  meaning, a writer of one is its author. Drawn from the whole volume map
  rather than the worklist's own candidate set, because a *documented* hot
  table still makes its producer worth describing.
* **impact** is write reach -- how many distinct tables those edges say it
  writes -- not the foreign-key in-degree a table uses. A routine has no
  foreign keys pointed at it; what it has is fan-out.
* **deficit** is `ROUTINE_DEFICIT_FIELDS`: of SW-1's five documentation fields,
  a routine can only lack one. It has no ownership assignment, no certification,
  no glossary link and no quality policy of its own, so claiming four more
  missing fields would inflate every routine's urgency against every table's.
  A constant term changes no order *within* the routine list, and makes a
  routine's score mean the same thing as a table's that lacks only its
  description.

Because usage is borrowed and impact is a different count, the two lists are
ranked separately and their ceilings are taken separately: interleaving them
would compare a borrowed number with a measured one and make each kind's
ceiling depend on how many of the other kind happened to be fetched.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Literal
from uuid import UUID

from aida.stewardship_worklist import score_item


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
    # SW-1 adoption (2026-09-04). The two factors a usage-only ranking cannot
    # see. Defaulted so every existing caller and test keeps working: a
    # signal gathered without them ranks exactly as it did before.
    #: How many other tables declare a foreign key into this one. A hub's
    #: meaning being wrong is wrong in every direction at once.
    downstream_count: int = 0
    #: Which of `stewardship_worklist.DEFICIT_FIELDS` this table lacks. AT-5
    #: previously treated "undocumented" as description-only, which ranked a
    #: described-but-unowned, uncertified, unlinked table as finished.
    missing: tuple[str, ...] = ()


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
    # SW-1 factors, on the response for the same reason `query_volume` is:
    # the order a steward is shown has to be explainable without reading
    # this file. `score` is the product; the three factors are its terms.
    score: float = 0.0
    usage: float = 0.0
    impact: float = 0.0
    deficit: float = 0.0
    downstream_count: int = 0
    missing: tuple[str, ...] = field(default_factory=tuple)


#: How AT-5 orders its candidates.
#:
#: `priority` is SW-1's `usage x impact x deficit`. It is a refinement of
#: `query_volume`, not a replacement for it -- usage is still a term, so a
#: table nobody queries still cannot reach the top -- but among comparably
#: used tables it puts the hub that is missing four of five fields ahead of
#: the leaf missing one.
#:
#: `query_volume` restores the pre-adoption order exactly. It exists so a
#: deployment that dislikes the new ordering can revert it with a query
#: parameter instead of a release.
WorklistRanking = Literal["priority", "query_volume"]


def rank_documentation_worklist(
    signals: list[TableQuerySignal],
    *,
    limit: int,
    offset: int = 0,
    include_zero_volume: bool = False,
    ranking: WorklistRanking = "priority",
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

    # Ceilings are taken over the *candidate set*, not a constant: "very used"
    # means very used for this estate. A fixed ceiling would make every table
    # in a quiet organization score near zero and rank arbitrarily.
    usage_ceiling = max(
        [signal.query_execution_count + signal.consumption_read_count for signal in candidates]
        + [1]
    )
    downstream_ceiling = max([signal.downstream_count for signal in candidates] + [1])

    scored: dict[UUID, tuple[float, float, float, float]] = {}
    for signal in candidates:
        # A signal gathered without SW-1's deficit fields has an empty
        # `missing`, which would score 0 and drop it. Falling back to
        # "description is the one thing known to be missing" keeps such a
        # caller ranking exactly as it did before adoption.
        missing = signal.missing or (("description",) if not signal.is_documented else ())
        scored[signal.table_id] = score_item(
            usage_references=signal.query_execution_count + signal.consumption_read_count,
            downstream_count=signal.downstream_count,
            missing=missing,
            usage_ceiling=usage_ceiling,
            downstream_ceiling=downstream_ceiling,
        )

    def _key(signal: TableQuerySignal) -> tuple[float, str, str]:
        if ranking == "query_volume":
            primary = float(-(signal.query_execution_count + signal.consumption_read_count))
        else:
            primary = -scored[signal.table_id][0]
        return (primary, signal.table_name.lower(), str(signal.table_id))

    ordered = sorted(candidates, key=_key)
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
            score=round(scored[signal.table_id][0], 6),
            usage=round(scored[signal.table_id][1], 6),
            impact=round(scored[signal.table_id][2], 6),
            deficit=round(scored[signal.table_id][3], 6),
            downstream_count=signal.downstream_count,
            missing=signal.missing,
        )
        for index, signal in enumerate(page)
    ]
    return entries, total


# ---------------------------------------------------------------------------
# R11-FP08: the same worklist, for routines
# ---------------------------------------------------------------------------

#: A routine's documentation checklist, against SW-1's five-field
#: `stewardship_worklist.DEFICIT_FIELDS`. See the module docstring: a routine
#: has no ownership assignment, certification, glossary link or quality policy
#: of its own, so `description` is the only one of the five it can be missing.
ROUTINE_DEFICIT_FIELDS: Final = ("description",)

#: `PACKAGE` is out of scope here as it is everywhere else in this codebase
#: (`routine_description_service.PACKAGE_NOT_DESCRIBABLE`, `footprint_gaps`,
#: `footprint_gap_detail`, `procedure_tool_blueprint`; packages wait on
#: R11-FP03). A package is a container for subprograms, not a callable unit
#: with a signature or lineage of its own, so there is nothing about it to
#: describe and nothing to rank it by.
PACKAGE_NOT_RANKABLE: Final = "PACKAGE_NOT_RANKABLE"


class RoutineNotRankable(ValueError):
    """A routine kind this worklist does not rank reached the ranker.

    Refused by name rather than skipped, `ensure_routines_are_describable`'s own
    rule: a steward who asks why a package never appears and gets an empty
    result has learned nothing about why. The gatherer
    (`documentation_worklist_signals.gather_routine_documentation_worklist_signals`)
    excludes packages in SQL so this never fires in normal operation; it exists
    so a caller assembling signals by hand cannot smuggle one past the rule.
    """

    def __init__(self, code: str, routine_ids: tuple[UUID, ...]) -> None:
        super().__init__(
            f"{code}: a package is a container for subprograms, not a callable unit "
            "with a signature or lineage of its own, so there is nothing here to "
            "describe. Rank the package's procedures and functions individually. "
            f"Offending routine ids: {', '.join(str(i) for i in routine_ids)}"
        )
        self.code = code
        self.routine_ids = routine_ids


@dataclass(frozen=True, slots=True)
class RoutineDocumentationSignal:
    """One routine's documentation-gap signal, gathered by the caller.

    DB-free for the reason `TableQuerySignal` is: the ranking below can be
    tested without a database and the caller owns every query.
    """

    routine_id: UUID
    routine_name: str
    schema_name: str
    datasource_name: str
    #: `PROCEDURE`, `FUNCTION`, or whatever a connector calls its callable unit.
    #: `PACKAGE` is refused -- see `RoutineNotRankable`.
    routine_type: str
    #: Borrowed usage: the combined query-execution + consumption-read volume of
    #: the tables this routine writes. Not the routine's own traffic, which is
    #: always zero -- see the module docstring.
    written_table_query_volume: int
    #: Write reach: distinct tables ACTIVE, non-intermediate lineage says it writes.
    writes_table_count: int
    #: Carried for the response only -- reads are not a ranking term, because a
    #: reader of a table is a consumer of its meaning, not its author.
    reads_table_count: int
    #: An APPROVED, non-withdrawn `RoutineDocumentationVersion` exists.
    is_documented: bool
    #: An open `RoutineDescriptionDraft` (`DRAFT`/`PENDING_APPROVAL`) exists --
    #: the state `uq_routine_description_draft_open` allows exactly one of. Carried
    #: rather than excluded, exactly as `TableQuerySignal.description_is_proposed`
    #: is: nothing has been approved, so the gap is real and a steward should still
    #: see it, while the consumer that would open a *second* draft (the steward
    #: agent's `SKIP_OPEN_DRAFT`) has the flag it needs to skip it.
    description_is_proposed: bool


@dataclass(frozen=True, slots=True)
class RoutineDocumentationWorklistEntry:
    """One ranked routine. `rank` and the four score terms are derived, kept on
    the row so the response and the order it reflects cannot disagree."""

    routine_id: UUID
    routine_name: str
    schema_name: str
    datasource_name: str
    routine_type: str
    rank: int
    written_table_query_volume: int
    writes_table_count: int
    reads_table_count: int
    description_is_proposed: bool
    score: float
    usage: float
    impact: float
    deficit: float


def rank_routine_documentation_worklist(
    signals: list[RoutineDocumentationSignal],
    *,
    limit: int,
    offset: int = 0,
    include_zero_volume: bool = False,
) -> tuple[list[RoutineDocumentationWorklistEntry], int]:
    """Rank undescribed routines by the value of what they write.

    The table ranking's rules, restated only where a routine changes them:

    - **Described routines are excluded entirely** (`is_documented`), not
      demoted -- this is the worklist, not a catalog view with a column.
    - **A routine whose description is only *proposed* stays in**, carrying the
      flag: nothing has been approved, so `is_documented` is False and the gap
      is real. The table rule, unchanged.
    - **Zero-volume routines are excluded by default.** A routine whose written
      tables nobody queries or reads has no real signal to rank it *by*, the
      same reason a zero-volume table is held back. Opting in sorts them after
      every real-volume row without a special case: usage is a term of a
      *product*, so a zero there is a zero overall and the row sorts last.
    - **Ties break deterministically** by name, then id, for stable pagination.
    - **`PACKAGE` is refused, not skipped** -- `RoutineNotRankable`.

    There is no `ranking` switch here. `WorklistRanking`'s `query_volume` mode
    exists to restore AT-5's *pre-SW-1* order for a deployment that dislikes the
    adoption; a routine list has no pre-adoption order to restore, and a second
    mode nobody has ever been shown is a knob with no revert to perform.

    Returns ``(page, total_candidates)`` on the same contract as
    `rank_documentation_worklist`.
    """
    packages = tuple(
        signal.routine_id
        for signal in signals
        if signal.routine_type.strip().upper() == "PACKAGE"
    )
    if packages:
        raise RoutineNotRankable(PACKAGE_NOT_RANKABLE, packages)

    candidates = [signal for signal in signals if not signal.is_documented]
    if not include_zero_volume:
        candidates = [
            signal for signal in candidates if signal.written_table_query_volume > 0
        ]

    # Ceilings over this candidate set, for the reason the table ranking takes
    # its own: "much used" means much used for this estate. Taken separately
    # from the table ceilings -- see the module docstring on why the two lists
    # are not interleaved.
    usage_ceiling = max([signal.written_table_query_volume for signal in candidates] + [1])
    writes_ceiling = max([signal.writes_table_count for signal in candidates] + [1])

    scored: dict[UUID, tuple[float, float, float, float]] = {}
    for signal in candidates:
        scored[signal.routine_id] = score_item(
            usage_references=signal.written_table_query_volume,
            downstream_count=signal.writes_table_count,
            missing=ROUTINE_DEFICIT_FIELDS,
            usage_ceiling=usage_ceiling,
            downstream_ceiling=writes_ceiling,
        )

    def _key(signal: RoutineDocumentationSignal) -> tuple[float, str, str]:
        return (
            -scored[signal.routine_id][0],
            signal.routine_name.lower(),
            str(signal.routine_id),
        )

    ordered = sorted(candidates, key=_key)
    total = len(ordered)
    page = ordered[offset : offset + limit]
    entries = [
        RoutineDocumentationWorklistEntry(
            routine_id=signal.routine_id,
            routine_name=signal.routine_name,
            schema_name=signal.schema_name,
            datasource_name=signal.datasource_name,
            routine_type=signal.routine_type,
            rank=offset + index + 1,
            written_table_query_volume=signal.written_table_query_volume,
            writes_table_count=signal.writes_table_count,
            reads_table_count=signal.reads_table_count,
            description_is_proposed=signal.description_is_proposed,
            score=round(scored[signal.routine_id][0], 6),
            usage=round(scored[signal.routine_id][1], 6),
            impact=round(scored[signal.routine_id][2], 6),
            deficit=round(scored[signal.routine_id][3], 6),
        )
        for index, signal in enumerate(page)
    ]
    return entries, total
