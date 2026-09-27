"""R11-FP03: a routine's captured definition history, readable at last.

`metadata_routine_definition_version` has appended an immutable row per first
capture and per moved definition since 2026-09-15, and nothing could read it.
A history that is written and never readable is the same as no history: a
steward asking "what changed in this procedure, and when?" had nowhere to
look, while two features already leaned on those rows -- R11-FP16 binds a
governed tool to the definition its SQL was copied from, and R11-FP08's
routine description names the definition version its prose describes. This
module is the read.

**The body is never served.** Not truncated, not summarised, not "for
reviewers only" -- not served. A routine body is the largest indirect-injection
surface in the estate (see the note on `MetadataRoutine.body_sql_redacted`), and
the value-free rules (ADR-0014, INV-6) mean the stored text is only ever
released through the existing screening gate. So this module reuses that gate --
`sql_redaction.VALUE_FREE_REDACTION_STATUSES` plus
`ingest_screening.is_eligible_for_model_context`, composed exactly as
`context_product_coverage._releasable` and `asset_description_service` compose
it -- and then *still* does not release the text. What the gate decides here is
whether a **digest** and a **derived table footprint** may be reported at all.

**What a steward actually needs is the diff, and it cannot be shown the text.**
So the answer is assembled out of what is genuinely value-free:

* the ordinal and the capture time, so the history is a timeline;
* the analysis run that captured it, so it can be tied back to a scan;
* availability, truncation and the change class;
* the digest of the stored value-free text *and the previous version's digest*,
  rather than a "changed" boolean -- the pair is self-checking, because
  `LITERAL_ONLY` means the stored text is byte-identical to its predecessor
  (`change_signals.code_change_signal`), so a LITERAL_ONLY row whose digests
  differ is a bug this read makes visible instead of hiding;
* whether the parse succeeded at that version, with the reason *codes* only;
* and the read/write table set **then versus now**.

That last one is the answer the feature exists for. "This procedure started
writing a second table on 12 September" is answerable without quoting a line of
SQL, and it is what a steward acts on.

**How far the footprint is taken, and how.** Nothing stores a per-version
footprint: `deep_procedure_lineage_edge` and `routine_parse_coverage` are both
measurements of the body *as last read* (`RoutineParseCoverage`'s own docstring
says so), replaced on every re-parse. So the footprint is **derived on read**,
by handing each version's stored value-free text to the same deterministic
`procedure_lineage.parse_procedure_lineage` the ingestion and agent paths use,
and keeping only the resolved table names. Four properties make that safe and
honest rather than a second lineage engine:

1. Nothing is written. No edge row, no coverage row, no review state. This is
   not governed lineage and `footprint_basis` says so, so a reader never
   mistakes it for an ADR-0026-reviewed edge.
2. Redaction does not change the answer. `sql_redaction`'s own docstring records
   that lineage does not depend on literal values, which is why the redacted
   text is what every lineage consumer already parses.
3. A `LITERAL_ONLY` version costs no parse at all: its stored text *is* its
   predecessor's, so its footprint is its predecessor's by construction. This is
   the same fact `change_signal_processing` relies on when it declines to
   re-examine lineage after a literal-only change.
4. It is budgeted. A page is bounded and so is the number of distinct texts one
   request will parse (`_FOOTPRINT_PARSE_BUDGET`); a version over budget reads
   `NOT_COMPUTED`, which tells a reader to narrow the window rather than letting
   a blank be read as "nothing changed".

**What is deliberately not served**, and must stay unserved: the body text in
any form; any excerpt, literal, constant or predicate from it; the per-statement
`unparsed_reason` suffixes, which can carry a callee name or a parse-error
message quoting a value (`routine_lineage_edges.unparsed_reason_codes` exists
for exactly this, and only its prefixes are reported); the column-level edges,
which are `procedure_lineage_api`'s reviewed surface and not a history; and
tables reached through a *called* routine, because `routine_call_descent` reads
the database and a read route deriving transitive lineage would be a second
lineage build. The footprint here is the routine's own statements.

**A withheld version keeps its row.** With its marker
(`column_description_model.WITHHELD`) and a reason code, never by omission:
dropping the row would let a reader infer something about the data from a fact
about their own entitlement, which is the rule `ProfilePanel`, the profile read
and `context_product_coverage._screened` all already follow.

**Authorization is per datasource, not per table.** A routine hangs off a schema
and has no parent table, so this follows the precedent that already answered
that question: `ontology_api._authorize_mapping_reads` gates a `ROUTINE` mapping
on `resource_type="datasource"`, and `routine_description_api._gate_routine`
does the same for a routine's description. See `_gate_routine` below.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.authorization_gate import gate_read
from aida.change_signals import CHANGE_LITERAL_ONLY
from aida.column_description_model import WITHHELD
from aida.config import Settings, get_settings
from aida.db import get_session
from aida.envelope_models import (
    AVAILABLE,
    MetadataRoutine,
    MetadataRoutineDefinitionVersion,
)
from aida.ingest_screening import is_eligible_for_model_context
from aida.models import DataSource, MetadataCatalog, MetadataSchema
from aida.procedure_lineage import parse_procedure_lineage
from aida.routine_lineage_edges import persistable_table, unparsed_reason_codes
from aida.schemas import RoutineDefinitionHistoryRead, RoutineDefinitionVersionRead
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES

router = APIRouter(prefix="/v1", tags=["routine-definition-history"])

#: Who may read one routine's definition history. Every read is also gated per
#: datasource through `gate_read`, so a role here is necessary, never
#: sufficient. Deliberately `routine_description_api.READ_ROLES`, unchanged: the
#: history says what a routine's body *state* was over time, which is strictly
#: less than the description read already composed from that body.
READ_ROLES: Final = (
    "PlatformAdmin",
    "MetadataAdmin",
    "DataAdmin",
    "SemanticAdmin",
    "DataSteward",
    "Reviewer",
    "Analyst",
    "Viewer",
    "Auditor",
)

#: How the read/write sets are arrived at, as a code the client renders into
#: words (the `ProfilePanel`/`observation_scope` rule: the server states the
#: fact, the reader's surface writes the sentence). One value today; it exists so
#: that a stored per-version footprint, if one is ever kept, can be told apart
#: from this derived one without changing the shape of the response.
FOOTPRINT_BASIS_REPARSED: Final = "REPARSED_STORED_DEFINITION"

#: The footprint was derived from this version's own stored text.
FOOTPRINT_COMPUTED: Final = "COMPUTED"
#: Derived, but the version before it was not, so nothing is claimed about what
#: this version *changed* -- only about what it reads and writes.
FOOTPRINT_COMPUTED_NO_BASELINE: Final = "COMPUTED_NO_BASELINE"
#: Only literals moved, so the stored text -- and therefore the footprint -- is
#: byte-identical to the predecessor's. No parse was needed to know that.
FOOTPRINT_UNCHANGED_LITERALS_ONLY: Final = "UNCHANGED_LITERALS_ONLY"
#: The screening gate withholds this version's stored text, so nothing may be
#: derived from it. The row stays, with its marker.
FOOTPRINT_WITHHELD: Final = "WITHHELD"
#: The source did not provide a body at this version, or none was stored.
FOOTPRINT_UNAVAILABLE: Final = "UNAVAILABLE"
#: Over this request's parse budget. Narrow the window; do not read it as "no
#: change".
FOOTPRINT_NOT_COMPUTED: Final = "NOT_COMPUTED"

#: Why a version's stored text may not be read, in the vocabulary
#: `routine_lineage_edges.RoutineNotEligibleError` already uses for the same
#: gate's refusals -- one set of codes for one decision, rather than a second
#: spelling of it.
WITHHELD_BODY_UNAVAILABLE: Final = "ROUTINE_BODY_UNAVAILABLE"
WITHHELD_BODY_NOT_STORED: Final = "ROUTINE_BODY_NOT_STORED"
WITHHELD_BODY_QUARANTINED: Final = "ROUTINE_BODY_QUARANTINED"
WITHHELD_BODY_MISSING: Final = "ROUTINE_BODY_MISSING"

#: Distinct stored texts one request will parse. The default page is smaller
#: than this, so the budget binds only on a deliberately wide window; a version
#: past it reads `NOT_COMPUTED` rather than silently reading as unchanged.
_FOOTPRINT_PARSE_BUDGET: Final = 25

#: Versions per page. Small by default because a definition history is read from
#: the recent end: the question is "what changed lately", and the older rows are
#: paged to.
_DEFAULT_LIMIT: Final = 20
_MAX_LIMIT: Final = 100


async def _gate_routine(
    session: AsyncSession,
    context: SecurityContext,
    settings: Settings,
    routine: MetadataRoutine,
) -> None:
    """The routine read gate, against the resource routine access is expressed on.

    `resource_type="datasource"`, following `ontology_api`'s `ROUTINE` branch and
    `routine_description_api._gate_routine`. A caller who cannot read a
    datasource's routines through `procedure_lineage_api` must not be able to
    read the history of one here.
    """
    await gate_read(
        session,
        context,
        settings,
        action="READ_METADATA",
        resource_type="datasource",
        resource_id=str(routine.datasource_id),
        datasource_id=routine.datasource_id,
    )


def _withheld_reason(version: MetadataRoutineDefinitionVersion) -> str | None:
    """`None` when this version's stored text may be read, else why it may not.

    The predicate is `context_product_coverage._releasable`'s, term for term --
    available, stored in a value-free form, and not quarantined by prompt-risk
    screening. It is spelled out rather than called so that the *reason* can be
    named: a reader who is told only "withheld" cannot tell "the source refused
    to give us the body" from "screening quarantined it", and those are
    different things to do next.
    """
    if version.availability != AVAILABLE:
        return WITHHELD_BODY_UNAVAILABLE
    if version.redaction_status not in VALUE_FREE_REDACTION_STATUSES:
        return WITHHELD_BODY_NOT_STORED
    if not is_eligible_for_model_context(version.screening_status):
        return WITHHELD_BODY_QUARANTINED
    if version.body_sql_redacted is None:
        return WITHHELD_BODY_MISSING
    return None


def _digest(version: MetadataRoutineDefinitionVersion) -> str | None:
    """SHA-256 of the *stored, value-free* text -- never of the original.

    `context_product_coverage._digest`'s rule, term for term: a digest of the
    literal-bearing original would be a fingerprint of source values, which is
    why the platform publishes this one and
    `MetadataRoutineDefinitionVersion.body_fingerprint` (which *is* of the raw
    text) stays internal to change detection.

    **Keyed on redaction status alone, deliberately**, which makes it wider than
    `_withheld_reason` above -- a quarantined version still reports a digest.
    That asymmetry is the platform's existing rule, not a new one: R11-FP12's
    coverage already reports `definition_digest` beside a false
    `definition_available`, because screening exists to keep prompt-risky *text*
    out of a model's context and a digest is not text. It is also the more
    useful answer: a steward who may not read a quarantined body can still see
    that the definition moved, which is the whole question this surface answers.
    What screening does govern is the footprint, because deriving one means
    parsing the text -- and `routine_lineage_edges.require_eligible_routine_body`
    already refuses to parse a quarantined body.
    """
    if version.body_sql_redacted is None:
        return None
    if version.redaction_status not in VALUE_FREE_REDACTION_STATUSES:
        return None
    return hashlib.sha256(version.body_sql_redacted.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class DefinitionFootprint:
    """What one version's own statements read and write, derived on read."""

    reads: frozenset[str]
    writes: frozenset[str]
    parse_completed: bool
    reason_codes: tuple[str, ...]


def derive_footprint(body: str, *, dialect: str) -> DefinitionFootprint:
    """Parse one stored definition and keep only its table footprint.

    Exposed (rather than private) because its two properties are what the
    surrounding module promises and a test has to be able to assert them
    directly: the routine's *own* statements only -- no descent into called
    routines, which reads the database -- and resolved, persistable table names
    only, so the parser's placeholders for a result set (`<RESULT>`) and for
    routine-local state (`<LOCAL>`) never reach a reader as if they were
    catalog tables. `persistable_table` is the same filter
    `routine_lineage_edges` applies before storing an edge.

    Intermediate edges -- a T-SQL `#temp`/`@table` or a `SELECT ... INTO`
    target, local to this body -- are dropped for the same reason
    `context_product_coverage.load_routine_references` drops them: they are not
    tables the outside world can see, so a change in one is not a change in what
    this procedure touches.

    Dropping the edge is not enough on its own, and this is the subtle part.
    `is_intermediate` marks an edge by its *target*, so the later statement that
    reads back out of a temp table (`#staging -> dbo.ledger`) is a perfectly
    ordinary edge whose source happens to be routine-local -- and reporting
    `staging` as a table this procedure reads would send a steward looking for
    something that does not exist. Those names are collected and excluded.
    Nothing is lost by it: the parser always synthesises the transitive edge
    across the hop (`via_temp_table`), so the real upstream table still appears.
    `context_product_coverage` never hits this because it keys reads on a
    resolved `source_table_id`, which a temp table has none of; a name-based
    footprint has to say so itself.
    """
    result = parse_procedure_lineage(body, dialect=dialect)
    routine_local = {
        edge.target_table.casefold() for edge in result.edges if edge.is_intermediate
    }
    reads: set[str] = set()
    writes: set[str] = set()
    for edge in result.edges:
        if edge.is_intermediate:
            continue
        source = persistable_table(edge.source_table, edge.source_resolved)
        if source is not None and source.casefold() not in routine_local:
            reads.add(source)
        if edge.is_write:
            target = persistable_table(edge.target_table, True)
            if target is not None and target.casefold() not in routine_local:
                writes.add(target)
    return DefinitionFootprint(
        reads=frozenset(reads),
        writes=frozenset(writes),
        parse_completed=result.is_fully_parsed,
        reason_codes=unparsed_reason_codes(result),
    )


async def _qualified_name(session: AsyncSession, routine: MetadataRoutine) -> str:
    """`catalog.schema.routine`, so two same-named procedures are told apart."""
    row = (
        await session.execute(
            select(MetadataCatalog.name, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.catalog_id == MetadataCatalog.id)
            .where(MetadataSchema.id == routine.schema_id)
        )
    ).first()
    if row is None:
        return routine.name
    return f"{row[0]}.{row[1]}.{routine.name}"


@dataclass(frozen=True, slots=True)
class _Resolved:
    """One version plus everything the response says about it, before shaping."""

    version: MetadataRoutineDefinitionVersion
    digest: str | None
    previous_digest: str | None
    withheld_reason: str | None
    state: str
    footprint: DefinitionFootprint | None
    reads_added: tuple[str, ...]
    reads_removed: tuple[str, ...]
    writes_added: tuple[str, ...]
    writes_removed: tuple[str, ...]


def _budgeted_digests(chain: list[MetadataRoutineDefinitionVersion]) -> set[str]:
    """Which distinct stored texts this request will parse, newest first.

    Spent from the newest end because that is the end a definition history is
    read from: if a budget has to bite, it must bite on the oldest rows, not on
    the change somebody is looking at today. Keyed by digest rather than by row
    so a run of `LITERAL_ONLY` versions -- which share one stored text -- costs
    one parse between them.
    """
    budgeted: set[str] = set()
    for version in reversed(chain):
        if _withheld_reason(version) is not None:
            continue
        digest = _digest(version)
        if digest is None or digest in budgeted:
            continue
        if len(budgeted) >= _FOOTPRINT_PARSE_BUDGET:
            break
        budgeted.add(digest)
    return budgeted


def _resolve_chain(
    chain: list[MetadataRoutineDefinitionVersion], *, dialect: str
) -> list[_Resolved]:
    """Walk the versions oldest to newest, carrying the previous footprint down.

    Oldest-first is what makes a diff possible at all: each row's answer is
    "what did this capture change", which is a statement about its predecessor.
    The caller supplies one row *below* the requested window as the anchor, so
    the oldest row on a page still has a baseline instead of reading as a first
    capture.
    """
    budgeted = _budgeted_digests(chain)
    cache: dict[str, DefinitionFootprint] = {}
    resolved: list[_Resolved] = []
    previous_digest: str | None = None
    previous_footprint: DefinitionFootprint | None = None
    on_page = False
    for version in chain:
        digest = _digest(version)
        withheld = _withheld_reason(version)
        # A recorded first capture, as the writer marks it: `change_class` is
        # NULL exactly for the version that had no predecessor
        # (`ingestion._record_routine_definition_versions`). Read from the row
        # rather than inferred from position, so page 3 of a history never
        # presents its oldest row as the beginning of one.
        first_capture = version.change_class is None
        footprint: DefinitionFootprint | None = None
        if withheld is not None:
            state = (
                FOOTPRINT_UNAVAILABLE
                if withheld in (WITHHELD_BODY_UNAVAILABLE, WITHHELD_BODY_MISSING)
                else FOOTPRINT_WITHHELD
            )
        elif digest is None:  # pragma: no cover - unreachable while the gate holds
            state = FOOTPRINT_UNAVAILABLE
        elif digest in cache:
            footprint = cache[digest]
            # LITERAL_ONLY means the stored text did not move
            # (`change_signals.code_change_signal` classes a change that way
            # exactly then), so the footprint is the predecessor's and saying
            # "only the literals moved" is more use than restating an identical
            # set. The state is claimed only when the row itself says
            # LITERAL_ONLY *and* the digests agree: a row whose class and
            # digests contradict each other falls through to COMPUTED, where
            # both digests are on show and the contradiction is visible rather
            # than smoothed over.
            state = (
                FOOTPRINT_UNCHANGED_LITERALS_ONLY
                if version.change_class == CHANGE_LITERAL_ONLY
                and previous_footprint is not None
                and digest == previous_digest
                else FOOTPRINT_COMPUTED
            )
        elif digest in budgeted:
            body = version.body_sql_redacted
            assert body is not None  # `_withheld_reason` returned None above
            footprint = derive_footprint(body, dialect=dialect)
            cache[digest] = footprint
            state = FOOTPRINT_COMPUTED
        else:
            state = FOOTPRINT_NOT_COMPUTED

        reads_added: tuple[str, ...] = ()
        reads_removed: tuple[str, ...] = ()
        writes_added: tuple[str, ...] = ()
        writes_removed: tuple[str, ...] = ()
        if footprint is not None:
            if previous_footprint is not None and not first_capture:
                reads_added = tuple(sorted(footprint.reads - previous_footprint.reads))
                reads_removed = tuple(sorted(previous_footprint.reads - footprint.reads))
                writes_added = tuple(sorted(footprint.writes - previous_footprint.writes))
                writes_removed = tuple(sorted(previous_footprint.writes - footprint.writes))
            elif first_capture:
                # The first definition Atlas ever captured: everything it
                # touches is new *to this history*, which is a true and useful
                # reading of "added". A later version whose predecessor was not
                # derived is not the same claim, and says so instead.
                reads_added = tuple(sorted(footprint.reads))
                writes_added = tuple(sorted(footprint.writes))
            elif state == FOOTPRINT_COMPUTED:
                state = FOOTPRINT_COMPUTED_NO_BASELINE

        resolved.append(
            _Resolved(
                version=version,
                digest=digest,
                previous_digest=None if first_capture or not on_page else previous_digest,
                withheld_reason=withheld,
                state=state,
                footprint=footprint,
                reads_added=reads_added,
                reads_removed=reads_removed,
                writes_added=writes_added,
                writes_removed=writes_removed,
            )
        )
        previous_digest = digest
        previous_footprint = footprint
        on_page = True
    return resolved


def _version_read(entry: _Resolved) -> RoutineDefinitionVersionRead:
    version = entry.version
    footprint = entry.footprint
    return RoutineDefinitionVersionRead(
        version_id=version.id,
        version_number=version.version_number,
        captured_at=version.captured_at,
        analysis_run_id=version.analysis_run_id,
        availability=version.availability,
        unavailable_reason=version.unavailable_reason,
        truncated=version.truncated,
        change_class=version.change_class,
        redaction_status=version.redaction_status,
        screening_status=version.screening_status,
        definition_digest=entry.digest,
        previous_definition_digest=entry.previous_digest,
        body_released=False,
        withheld_marker=WITHHELD if entry.withheld_reason is not None else None,
        withheld_reason_code=entry.withheld_reason,
        footprint_state=entry.state,
        parse_completed=None if footprint is None else footprint.parse_completed,
        unparsed_reason_codes=[] if footprint is None else list(footprint.reason_codes),
        reads_table_names=[] if footprint is None else sorted(footprint.reads),
        writes_table_names=[] if footprint is None else sorted(footprint.writes),
        reads_added=list(entry.reads_added),
        reads_removed=list(entry.reads_removed),
        writes_added=list(entry.writes_added),
        writes_removed=list(entry.writes_removed),
    )


@router.get(
    "/routines/{routine_id}/definition-history",
    response_model=RoutineDefinitionHistoryRead,
)
async def get_routine_definition_history(
    routine_id: UUID,
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
    context: SecurityContext = Depends(require_roles(*READ_ROLES)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RoutineDefinitionHistoryRead:
    """Every captured definition of one routine, newest first, without its text.

    R11-FP03. Newest first because the question is "what changed lately"; the
    `version_number` on each row keeps the true order explicit, so a reader is
    never left inferring it from position.

    An empty list means this routine has no captured definition at all -- a
    package member, whose source is its package's, is the ordinary case -- and
    is a different answer from a 404, which means no such routine. Neither is
    ever answered with a made-up version 1.
    """
    routine = await session.get(MetadataRoutine, routine_id)
    if routine is None:
        raise HTTPException(status_code=404, detail="routine not found")
    # INV-5, restated here rather than inherited: the row was fetched by primary
    # key, which crosses organizations, so the tenancy check is the thing that
    # makes this read tenant-scoped at all.
    enforce_organization(context, routine.organization_id)
    await _gate_routine(session, context, settings, routine)

    datasource = await session.get(DataSource, routine.datasource_id)
    if datasource is None:  # pragma: no cover - FK-guaranteed
        raise HTTPException(status_code=404, detail="datasource for this routine not found")

    scoped = select(MetadataRoutineDefinitionVersion).where(
        MetadataRoutineDefinitionVersion.routine_id == routine.id,
        MetadataRoutineDefinitionVersion.organization_id == routine.organization_id,
    )
    total = await session.scalar(select(func.count()).select_from(scoped.subquery())) or 0
    # One row *below* the window as well, so the oldest row on the page is
    # diffed against its real predecessor instead of reading as a first capture.
    # `limit + 1` from `offset`, ordered newest first, is that row.
    window = list(
        (
            await session.scalars(
                scoped.order_by(MetadataRoutineDefinitionVersion.version_number.desc())
                .limit(limit + 1)
                .offset(offset)
            )
        ).all()
    )
    anchored = len(window) > limit
    chain = list(reversed(window))
    resolved = _resolve_chain(chain, dialect=datasource.dialect)
    if anchored:
        resolved = resolved[1:]

    return RoutineDefinitionHistoryRead(
        routine_id=routine.id,
        routine_qualified_name=await _qualified_name(session, routine),
        routine_type=routine.routine_type,
        signature=routine.signature,
        status=routine.status,
        dialect=datasource.dialect,
        footprint_basis=FOOTPRINT_BASIS_REPARSED,
        footprint_parse_budget=_FOOTPRINT_PARSE_BUDGET,
        versions=[_version_read(entry) for entry in reversed(resolved)],
        limit=limit,
        offset=offset,
        total=total,
    )


__all__ = [
    "DefinitionFootprint",
    "derive_footprint",
    "get_routine_definition_history",
    "router",
]
