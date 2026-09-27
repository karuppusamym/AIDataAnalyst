"""The captured-body lineage tables: their gate and their writes.

`deep_procedure_lineage_edge` holds what `procedure_lineage.parse_procedure_lineage`
finds in one captured `MetadataRoutine` body. Two callers write it -- a person's
parse (`procedure_lineage_api`) and the lineage agent (ADR-0029) -- and they share
what lives here, moved out of that router on 2026-09-11 so the agent reaches it
without importing one:

* `require_eligible_routine_body`, the gate every use of a captured body passes
  first (`procedure_tool_blueprint` uses it too);
* `routine_edge_row`, one parsed edge as a row, its table ids resolved by
  `lineage_table_resolution` -- the router's identical private copy of that
  resolver is gone;
* `persist_routine_edges`, a person's parse, written under ADR-0026's review
  mode the way every other parser's already is;
* `record_routine_parse_coverage`, the per-object coverage measurement both
  writers record (review 2026-09-16, finding F06.4) so that "was this routine
  fully understood?" is a stored answer rather than something re-derived by
  hunting the edge table for `UNPARSED` rows.

**Triggers joined it on 2026-09-17 (R11-FP01).** A trigger body is the same kind
of artifact as a routine body -- redacted, fingerprinted and screened by the same
four columns -- so it passes the same gate, spelled `require_eligible_trigger_body`
so a refusal says which axis refused. Two things are genuinely different and both
live here because both need the catalog:

* **The subject.** A trigger's lineage has a source the body never names: the
  firing table, which `MetadataTrigger.table_name` holds. `trigger_body` resolves
  it to a qualified name and `parse_trigger_lineage` binds it, so an edge out of
  `NEW`/`INSERTED` is an edge out of that table.
* **PostgreSQL's action routine.** A PostgreSQL trigger has no body at all; the
  code is in the function `action_routine` names, which is discovered on the
  *routine* axis. The join belongs here rather than on that axis, because the
  routine axis cannot do it: a trigger function's `NEW` means nothing without a
  firing table, and one function may be attached to several tables, so the
  binding exists only per trigger. `trigger_body` does that lookup, scoped to the
  trigger's own organization and datasource, and reports a callee it could not
  reach in `routine_call_descent`'s own vocabulary so the footprint gap register
  already knows how to route it.

`record_trigger_parse_coverage` is `record_routine_parse_coverage` for that axis,
over one shared `_measure`, so "fully understood" means one thing on both.

**Statement ranges and package members (2026-09-18).** Both row builders copy an
edge's statement range (R11-FP07) -- positions and the digest of the stored body
they index, never an excerpt -- and a routine row copies its package-member
attribution (R11-FP03). `resolve_package_member_ids` turns a package parse's
members into the captured member routines they are, and `reconcile_decided_edges`
re-points a decided edge the new parse finds again at where it now is, since a
position describes the current body, not the decision.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import (
    AVAILABLE,
    MetadataRoutine,
    MetadataRoutineParameter,
    MetadataTrigger,
)
from aida.ingest_screening import is_eligible_for_model_context
from aida.lineage_table_resolution import resolve_lineage_table_ids
from aida.models import DataSource, MetadataSchema
from aida.parsed_lineage_review_service import resolve_review_status_for_new_edge
from aida.procedure_lineage import (
    PROCEDURE_LOCAL_TARGET,
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    StatementRangeStatus,
    UnparsedReason,
    unparsed_marker_result,
)
from aida.procedure_lineage_models import (
    DeepProcedureLineageEdge,
    RoutineParseCoverage,
    TriggerLineageEdge,
    TriggerParseCoverage,
)
from aida.sql_lineage_parser import PROCEDURE_RESULT_TARGET
from aida.sql_redaction import VALUE_FREE_REDACTION_STATUSES

#: The positional precision a coverage record says its parse's statements are
#: located to. R11-FP07: a line/column span in the *stored* body, pinned by the
#: SHA-256 of that text. The engine capability matrix used to record this as
#: unsupported because a range into the customer's source cannot be kept -- the
#: platform does not hold that text (R11-D16) -- and it still cannot be. What can
#: be kept is a range into the text the platform does hold and parsed, which is
#: the text a steward reviewing the routine in Atlas is shown.
SOURCE_MAPPING_GRANULARITY = "STATEMENT_RANGE"
#: The precision of a parse that reached no body to locate anything in -- a
#: trigger whose action routine could not be read -- and of every coverage row
#: measured before R11-FP07.
UNLOCATED_SOURCE_MAPPING_GRANULARITY = "STATEMENT_ORDINAL"

#: Cap on the joined reason-code summary, matching the column's own width.
_MAX_REASON_CODES_LENGTH = 400

#: The table's natural key within one routine: statement ordinal, source,
#: target, transformation, and the temp table a transitive edge runs through.
RoutineEdgeKey = tuple[int, str, str, str, str, str, str | None]


class CapturedBodyNotEligibleError(ValueError):
    """A captured definition body is missing, withheld, unparsed or quarantined --
    refused, never guessed. Mirrors `view_tool_blueprint.py`'s
    `ViewNotEligibleError` gate exactly. Subclassed per axis so a caller can
    catch one axis's refusal, and so the code says which axis refused."""

    #: How the refusal message names the object.
    noun = "object"
    #: The code a caller that did not name one gets.
    default_code = "BODY_NOT_ELIGIBLE"

    def __init__(self, reason: str, *, code: str | None = None) -> None:
        self.reason = reason
        #: Stable and value-free, for a caller that records why rather than says it (R11-FP14).
        self.code = code or self.default_code
        super().__init__(f"{self.noun} is not eligible for lineage parsing: {reason}")


class RoutineNotEligibleError(CapturedBodyNotEligibleError):
    """The routine's own captured body cannot be parsed."""

    noun = "routine"
    default_code = "ROUTINE_BODY_NOT_ELIGIBLE"


class TriggerNotEligibleError(CapturedBodyNotEligibleError):
    """The trigger's own captured body cannot be parsed. Note what this is *not*:
    a PostgreSQL trigger has no body of its own by design, and `trigger_body`
    follows its `action_routine` instead of raising -- this is raised only when a
    trigger that should carry a body does not, or when the one it carries is
    withheld, unstored or quarantined."""

    noun = "trigger"
    default_code = "TRIGGER_BODY_NOT_ELIGIBLE"


def _require_eligible_body(
    *,
    error: type[CapturedBodyNotEligibleError],
    prefix: str,
    present: bool,
    status: str,
    availability: str,
    unavailable_reason: str | None,
    redaction_status: str,
    screening_status: str,
    body: str | None,
) -> str:
    """The five checks every captured body passes before anything reads it.

    Shared by the routine and the trigger axis rather than copied, because a body
    that is safe to parse is the same question on both: the same four columns
    decide it, and a check that existed on one axis and not the other is exactly
    how a quarantined body reaches a parser. `prefix` spells the axis into each
    code (`ROUTINE_INACTIVE`, `TRIGGER_INACTIVE`) so a caller that records the
    code still knows which axis refused.
    """
    noun = error.noun
    if not present:
        raise error(f"no captured {noun} for this id", code=f"{prefix}_MISSING")
    if status != "ACTIVE":
        raise error(f"{noun} status is {status}, not ACTIVE", code=f"{prefix}_INACTIVE")
    if availability != AVAILABLE:
        raise error(
            f"{noun} body is UNAVAILABLE ({unavailable_reason or 'no reason recorded'})",
            code=f"{prefix}_BODY_UNAVAILABLE",
        )
    # `LEXICAL` text is as value-free as `PARSED` text; it is the tier that exists so
    # procedure bodies sqlglot cannot read as one statement -- every PL/pgSQL routine,
    # every Snowflake script -- stay parseable for lineage. Only `UNPARSED` stores nothing.
    if redaction_status not in VALUE_FREE_REDACTION_STATUSES:
        raise error(
            f"{noun} body redaction status is {redaction_status}, not PARSED or LEXICAL",
            code=f"{prefix}_BODY_NOT_STORED",
        )
    if not is_eligible_for_model_context(screening_status):
        raise error(
            f"{noun} body is quarantined by prompt-risk screening "
            f"(screening_status={screening_status})",
            code=f"{prefix}_BODY_QUARANTINED",
        )
    if body is None:
        raise error(
            f"{noun} has no body text despite AVAILABLE status", code=f"{prefix}_BODY_MISSING"
        )
    return body


def require_eligible_routine_body(routine: MetadataRoutine | None) -> str:
    """Return the routine's own redacted body text, or raise
    `RoutineNotEligibleError` naming exactly why it cannot be parsed."""
    return _require_eligible_body(
        error=RoutineNotEligibleError,
        prefix="ROUTINE",
        present=routine is not None,
        status="" if routine is None else routine.status,
        availability="" if routine is None else routine.availability,
        unavailable_reason=None if routine is None else routine.unavailable_reason,
        redaction_status="" if routine is None else routine.redaction_status,
        screening_status="" if routine is None else routine.screening_status,
        body=None if routine is None else routine.body_sql_redacted,
    )


def require_eligible_trigger_body(trigger: MetadataTrigger | None) -> str:
    """Return the trigger's own redacted body text, or raise
    `TriggerNotEligibleError` naming exactly why it cannot be parsed."""
    return _require_eligible_body(
        error=TriggerNotEligibleError,
        prefix="TRIGGER",
        present=trigger is not None,
        status="" if trigger is None else trigger.status,
        availability="" if trigger is None else trigger.availability,
        unavailable_reason=None if trigger is None else trigger.unavailable_reason,
        redaction_status="" if trigger is None else trigger.redaction_status,
        screening_status="" if trigger is None else trigger.screening_status,
        body=None if trigger is None else trigger.body_sql_redacted,
    )


def persistable_table(name: str, resolved: bool) -> str | None:
    """`name`, if it can be a catalog table: resolved, and not the parser's
    placeholder for a statement's result set or for routine-local state."""
    if not resolved or name in (PROCEDURE_RESULT_TARGET, PROCEDURE_LOCAL_TARGET):
        return None
    return name


def routine_edge_key(
    edge: ProcedureLineageEdgeRecord | DeepProcedureLineageEdge,
) -> RoutineEdgeKey:
    return (
        edge.statement_ordinal,
        edge.source_table,
        edge.source_column,
        edge.target_table,
        edge.target_column,
        edge.transformation_type,
        edge.via_temp_table,
    )


#: Review state of a decided edge a complete re-parse no longer produces. Already one of
#: `parsed_lineage_review_service.REVIEW_STATUSES`, so the review queue can filter on it.
SUPERSEDED = "SUPERSEDED"


def _natural_key(edge: Any) -> tuple[Any, ...]:
    """`routine_edge_key` and `trigger_edge_key` are one tuple over the same attributes; this is
    that tuple for a parsed record or a row of either table."""
    return (
        edge.statement_ordinal,
        edge.source_table,
        edge.source_column,
        edge.target_table,
        edge.target_column,
        edge.transformation_type,
        edge.via_temp_table,
    )


@dataclass(frozen=True, slots=True)
class DecidedEdgeReconciliation:
    """What one re-parse did to edges a person had already decided."""

    #: ACTIVE edges the complete new parse no longer produces, now SUPERSEDED.
    superseded: tuple[Any, ...] = ()
    #: SUPERSEDED edges the new parse produces again, back to PROPOSED for a person to decide.
    revived: tuple[Any, ...] = ()


async def reconcile_decided_edges(
    session: AsyncSession,
    *,
    model: type[DeepProcedureLineageEdge] | type[TriggerLineageEdge],
    datasource: DataSource,
    owner_id: UUID,
    result: ProcedureParseResult,
    produced: Iterable[ProcedureLineageEdgeRecord],
) -> DecidedEdgeReconciliation:
    """Bring one routine's or trigger's decided edges in line with its new parse.

    Before this, a re-parse under `require_review` kept every decided edge as it was, so an edge
    a person approved went on steering impact analysis after the body stopped writing it -- the
    R11-FP01 remainder "no routine-backed axis marks such an edge SUPERSEDED".

    * An ACTIVE edge the new parse does not produce becomes SUPERSEDED -- **only after a
      complete parse**. If any statement went unparsed, or the body could not be read, an edge
      missing from the result may be the parser's gap rather than the body's change, and
      retiring approved lineage on that evidence would be wrong; it stays ACTIVE until a parse
      that understood the whole body says otherwise.
    * A SUPERSEDED edge the new parse produces again goes back to PROPOSED. The body writes it
      once more, but what was approved was an earlier body, so a person decides again rather
      than the old approval silently returning.

    Gap markers and REJECTED edges are never touched: a rejection stays a rejection. Every
    statement restates the organization and the datasource (INV-5).
    """
    decided: Sequence[Any]
    if model is DeepProcedureLineageEdge:
        decided = (
            await session.scalars(
                select(DeepProcedureLineageEdge).where(
                    DeepProcedureLineageEdge.organization_id == datasource.organization_id,
                    DeepProcedureLineageEdge.datasource_id == datasource.id,
                    DeepProcedureLineageEdge.routine_id == owner_id,
                    DeepProcedureLineageEdge.review_status.in_(("ACTIVE", SUPERSEDED)),
                    DeepProcedureLineageEdge.transformation_type != UNPARSED_TRANSFORMATION_TYPE,
                )
            )
        ).all()
    else:
        decided = (
            await session.scalars(
                select(TriggerLineageEdge).where(
                    TriggerLineageEdge.organization_id == datasource.organization_id,
                    TriggerLineageEdge.datasource_id == datasource.id,
                    TriggerLineageEdge.trigger_id == owner_id,
                    TriggerLineageEdge.review_status.in_(("ACTIVE", SUPERSEDED)),
                    TriggerLineageEdge.transformation_type != UNPARSED_TRANSFORMATION_TYPE,
                )
            )
        ).all()
    by_key = {_natural_key(edge): edge for edge in produced}
    keys = set(by_key)
    superseded = (
        [row for row in decided if row.review_status == "ACTIVE" and _natural_key(row) not in keys]
        if result.is_fully_parsed
        else []
    )
    revived = [
        row for row in decided
        if row.review_status == SUPERSEDED and _natural_key(row) in keys
    ]
    for row in superseded:
        row.review_status = SUPERSEDED
    for row in revived:
        row.review_status = "PROPOSED"
    # R11-FP07: a decided edge the new parse finds again is re-pointed at where its
    # statement now is. A person decided the *fact*; the range says where the fact
    # sits in the body as it stands, and a comment added above it moves every line.
    # A superseded edge is left pointing into the body it was found in -- its digest
    # no longer matches the stored body, which is how a reader tells.
    for row in decided:
        found = by_key.get(_natural_key(row))
        if found is not None:
            apply_statement_range(row, found)
    return DecidedEdgeReconciliation(superseded=tuple(superseded), revived=tuple(revived))


def apply_statement_range(
    row: DeepProcedureLineageEdge | TriggerLineageEdge, edge: ProcedureLineageEdgeRecord
) -> None:
    """Copy `edge`'s statement range, and its source and target token ranges, onto
    `row` (R11-FP07).

    Positions and the digest of the text they index -- never the text. An edge
    with no range writes NULL positions and its NOT_LOCATED status, never zeros.
    """
    where = edge.statement_range
    row.statement_start_offset = where.start_offset if where else None
    row.statement_end_offset = where.end_offset if where else None
    row.statement_start_line = where.start_line if where else None
    row.statement_start_column = where.start_column if where else None
    row.statement_end_line = where.end_line if where else None
    row.statement_end_column = where.end_column if where else None
    row.statement_range_status = (
        edge.statement_range_status if where else StatementRangeStatus.NOT_LOCATED.value
    )
    row.statement_text_digest = edge.statement_text_digest if where else None
    # R11-FP07 token grain: a token range refines the statement range and indexes
    # the same text, so it is written only alongside one.
    source = edge.source_token_range if where else None
    target = edge.target_token_range if where else None
    row.source_token_start_offset = source.start_offset if source else None
    row.source_token_end_offset = source.end_offset if source else None
    row.source_token_kind = source.kind if source else None
    row.target_token_start_offset = target.start_offset if target else None
    row.target_token_end_offset = target.end_offset if target else None
    row.target_token_kind = target.kind if target else None


async def resolve_routine_table_ids(
    session: AsyncSession, datasource_id: UUID, edges: Iterable[ProcedureLineageEdgeRecord]
) -> dict[str, UUID]:
    """Catalog ids for every name in `edges` that can be a table."""
    names = {
        name
        for edge in edges
        for name in (
            persistable_table(edge.source_table, edge.source_resolved),
            persistable_table(edge.target_table, True),
        )
        if name is not None
    }
    return await resolve_lineage_table_ids(session, datasource_id, names)


def routine_edge_row(
    edge: ProcedureLineageEdgeRecord,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    routine_id: UUID,
    sql_hash: str,
    table_ids: dict[str, UUID],
    review_status: str,
    created_by: str | None,
    member_routine_ids: Mapping[int, UUID] | None = None,
) -> DeepProcedureLineageEdge:
    """One parsed edge as a row. R11-FP03: `member_routine_ids` maps a package
    parse's statement ordinals to the captured member routine each belongs to
    (`resolve_package_member_ids`); a caller that does not pass it still records
    the member by name and grain, and leaves the id NULL rather than guessing.

    The same map resolves `via_routine_id` for an edge spliced from an in-package
    sibling call: `edge.via_routine_locator` is an ordinal inside the *callee*
    member's own span, so looking it up in `member_routine_ids` (built over this
    same parse) gives the callee's id, never the caller's -- which
    `edge.package_member`'s own ordinal would give instead. A cross-routine splice
    already carries its callee's id directly on `edge.via_routine_id`, resolved
    by `aida.routine_call_descent` at descent time, and wins if somehow both are
    set (they never should be)."""
    source_name = persistable_table(edge.source_table, edge.source_resolved)
    target_name = persistable_table(edge.target_table, True)
    via_routine_id = edge.via_routine_id
    if via_routine_id is None and edge.via_routine_locator is not None:
        via_routine_id = (member_routine_ids or {}).get(edge.via_routine_locator)
    row = DeepProcedureLineageEdge(
        organization_id=organization_id,
        datasource_id=datasource_id,
        routine_id=routine_id,
        statement_ordinal=edge.statement_ordinal,
        source_table=edge.source_table,
        source_column=edge.source_column,
        target_table=edge.target_table,
        target_column=edge.target_column,
        source_resolved=edge.source_resolved,
        source_table_id=table_ids.get(source_name) if source_name else None,
        target_table_id=table_ids.get(target_name) if target_name else None,
        transformation_type=edge.transformation_type,
        confidence=edge.confidence,
        dialect=edge.dialect,
        is_write=edge.is_write,
        is_intermediate=edge.is_intermediate,
        control_flow_context=edge.control_flow_context,
        unparsed_reason=edge.unparsed_reason,
        via_temp_table=edge.via_temp_table,
        via_routine=edge.via_routine,
        via_routine_id=via_routine_id,
        package_member=edge.package_member,
        member_attribution=edge.member_attribution,
        member_routine_id=(
            (member_routine_ids or {}).get(edge.statement_ordinal)
            if edge.package_member is not None
            else None
        ),
        sql_hash=sql_hash,
        review_status=review_status,
        created_by=created_by,
    )
    apply_statement_range(row, edge)
    return row


async def resolve_package_member_ids(
    session: AsyncSession,
    *,
    datasource: DataSource,
    package: MetadataRoutine,
    result: ProcedureParseResult,
) -> dict[int, UUID]:
    """R11-FP03: statement ordinal -> the captured member routine it belongs to.

    A member is the routine captured under this package (`package_name`) in the
    package's own schema with the member's name. Two overloads share a name, so
    they are told apart by their ordered parameter names -- PL/SQL requires a
    body's subprogram header to repeat its spec's, which is what the catalog
    records. A member that still matches more than one routine, or none (not
    captured, or a private subprogram the spec does not declare), maps to
    nothing: its edges keep the member's name and grain, and no id is guessed.
    Every query restates the organization and the datasource (INV-5).
    """
    if not result.package_members:
        return {}
    candidates = (
        await session.scalars(
            select(MetadataRoutine).where(
                MetadataRoutine.organization_id == datasource.organization_id,
                MetadataRoutine.datasource_id == datasource.id,
                MetadataRoutine.schema_id == package.schema_id,
                MetadataRoutine.package_name == package.name,
                MetadataRoutine.status == "ACTIVE",
            )
        )
    ).all()
    if not candidates:
        return {}
    parameters: dict[UUID, list[tuple[int, str]]] = {}
    for parameter in (
        await session.scalars(
            select(MetadataRoutineParameter).where(
                MetadataRoutineParameter.organization_id == datasource.organization_id,
                MetadataRoutineParameter.datasource_id == datasource.id,
                MetadataRoutineParameter.routine_id.in_([row.id for row in candidates]),
            )
        )
    ).all():
        parameters.setdefault(parameter.routine_id, []).append(
            (parameter.ordinal_position, (parameter.name or "").lower())
        )
    signature = {
        routine_id: tuple(name for _position, name in sorted(entries))
        for routine_id, entries in parameters.items()
    }
    resolved: dict[int, UUID] = {}
    for member in result.package_members:
        if member.first_ordinal is None or member.last_ordinal is None:
            continue
        named = [row for row in candidates if row.name.lower() == member.name.lower()]
        if len(named) > 1:
            wanted = tuple(name.lower() for name in member.parameter_names)
            named = [row for row in named if signature.get(row.id, ()) == wanted]
        if len(named) != 1:
            continue
        for ordinal in range(member.first_ordinal, member.last_ordinal + 1):
            resolved[ordinal] = named[0].id
    return resolved


def unparsed_reason_codes(result: ProcedureParseResult) -> tuple[str, ...]:
    """The distinct `UnparsedReason` codes one parse produced, sorted.

    `ProcedureParseResult.errors` holds each unparsed chunk's reason as a
    prefix plus, where useful, a short suffix carrying the specific detail --
    a parse-error message, a node type name, a callee name. Only the prefix is
    taken here, matched against the real enum rather than split on a
    separator, so a summary can never carry the detail: a callee name is a
    source identifier and a parse-error text can quote a value (INV-6).

    A reason that matches no enum member is dropped rather than stored as
    free text. The count of unparsed statements is recorded separately, so
    dropping an unrecognised code loses the label, never the gap.
    """
    known = {reason.value for reason in UnparsedReason}
    found = {
        reason
        for error in result.errors
        for reason in known
        if error == reason or error.startswith(f"{reason}:") or error.startswith(f"{reason} ")
    }
    return tuple(sorted(found))


async def record_routine_parse_coverage(
    session: AsyncSession,
    *,
    datasource: DataSource,
    routine: MetadataRoutine,
    result: ProcedureParseResult,
    measured_by: str | None,
) -> RoutineParseCoverage:
    """Store how completely `routine`'s body was understood by this parse.

    One row per routine, replaced in place on a re-parse: this is a
    measurement of the body as last read, not a history. Both writers of
    procedure lineage call it -- a person's parse and the lineage agent -- so
    the answer does not depend on which of them last looked at the routine.

    The row keeps `parse_completed` and `is_read_only` as the booleans
    `ProcedureParseResult` computes. Nothing here writes a state string:
    `capability_states.parse_coverage_state` renders the pair at the reporting
    boundary, which is the rule that keeps a stored sentinel from ever standing
    in for a real value.
    """
    existing = (
        await session.scalars(
            select(RoutineParseCoverage).where(
                RoutineParseCoverage.datasource_id == datasource.id,
                RoutineParseCoverage.routine_id == routine.id,
            )
        )
    ).first()
    row = existing or RoutineParseCoverage(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        routine_id=routine.id,
    )
    _measure(row, result, measured_by=measured_by)
    if existing is None:
        session.add(row)
    return row


def _measure(
    row: RoutineParseCoverage | TriggerParseCoverage,
    result: ProcedureParseResult,
    *,
    measured_by: str | None,
) -> None:
    """Write one parse's measurement onto a coverage row, routine or trigger.

    One function for both axes because the two tables are one shape on purpose:
    a trigger body is parsed by the same parser, so "fully understood" must mean
    the same thing on both, down to which reason codes are kept and which are
    dropped (`unparsed_reason_codes` -- prefixes only, INV-6)."""
    unparsed_count = sum(
        1
        for edge in result.edges
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
    )
    row.parse_completed = result.is_fully_parsed
    row.is_read_only = result.is_read_only
    row.statement_count = result.statement_count
    row.unparsed_statement_count = unparsed_count
    row.unparsed_reason_codes = ",".join(unparsed_reason_codes(result))[
        :_MAX_REASON_CODES_LENGTH
    ]
    row.dialect = result.dialect
    row.confidence = result.confidence
    row.sql_hash = result.sql_hash
    # R11-FP07: a parse that read a body located its statements in it; one that
    # reached no body (an unreachable action routine) located nothing.
    row.source_mapping_granularity = (
        SOURCE_MAPPING_GRANULARITY
        if result.statement_text_digest is not None
        else UNLOCATED_SOURCE_MAPPING_GRANULARITY
    )
    row.parsed_at = datetime.now(UTC)
    row.measured_by = measured_by
    # R11-FP03: which grain a package's edges were attributed at, and why a
    # package fell back to the whole-package grain. NULL for anything else.
    row.member_attribution = result.member_attribution
    row.member_fallback_reason = result.member_fallback_reason


async def persist_routine_edges(
    session: AsyncSession,
    *,
    datasource: DataSource,
    routine: MetadataRoutine,
    result: ProcedureParseResult,
    review_mode: str,
    threshold: float,
    created_by: str | None,
) -> int:
    """A person's parse of one routine, written under ADR-0026's review mode.
    Returns the rows written.

    `auto_active`, the default, keeps what this table always did: the routine's
    rows are replaced by its re-parse -- AT-D2's delete-then-insert, scoped to
    this routine and never touching another's -- and every one is ACTIVE.

    `require_review` lands each edge as every other parser's are
    (`resolve_review_status_for_new_edge`), and a re-parse replaces only what
    is undecided: PROPOSED rows and UNPARSED markers. A key already present in
    any other state -- approved, activated by the confidence threshold, or
    rejected by a reviewer -- is left as it is and not written again.

    An UNPARSED marker records a gap in the parse, not an edge, so it is never
    put in front of a reviewer: it is ACTIVE in either mode.
    """
    clear = delete(DeepProcedureLineageEdge).where(
        DeepProcedureLineageEdge.datasource_id == datasource.id,
        DeepProcedureLineageEdge.routine_id == routine.id,
    )
    if review_mode == "require_review":
        clear = clear.where(
            or_(
                DeepProcedureLineageEdge.review_status == "PROPOSED",
                DeepProcedureLineageEdge.transformation_type == UNPARSED_TRANSFORMATION_TYPE,
            )
        )
    await session.execute(clear)
    if review_mode == "require_review":
        # Decided edges follow the body: approved lineage it no longer writes is superseded
        # after a complete parse, and superseded lineage it writes again is proposed again.
        await reconcile_decided_edges(
            session,
            model=DeepProcedureLineageEdge,
            datasource=datasource,
            owner_id=routine.id,
            result=result,
            produced=result.edges,
        )
    if not result.edges:
        return 0

    kept: set[RoutineEdgeKey] = set()
    if review_mode == "require_review":
        kept = {
            routine_edge_key(row)
            for row in (
                await session.scalars(
                    select(DeepProcedureLineageEdge).where(
                        DeepProcedureLineageEdge.datasource_id == datasource.id,
                        DeepProcedureLineageEdge.routine_id == routine.id,
                    )
                )
            ).all()
        }
    table_ids = await resolve_routine_table_ids(session, datasource.id, result.edges)
    # R11-FP03: a package's edges name the member routine they belong to.
    member_routine_ids = await resolve_package_member_ids(
        session, datasource=datasource, package=routine, result=result
    )
    written = 0
    for edge in result.edges:
        key = routine_edge_key(edge)
        if key in kept:
            continue
        kept.add(key)
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE:
            review_status = "ACTIVE"
        else:
            review_status = resolve_review_status_for_new_edge(
                review_mode=review_mode,
                confidence=edge.confidence,
                threshold=threshold,
                source_trusted=None,  # a captured body is parsed here, never pushed
            )
        session.add(
            routine_edge_row(
                edge,
                organization_id=datasource.organization_id,
                datasource_id=datasource.id,
                routine_id=routine.id,
                sql_hash=result.sql_hash,
                table_ids=table_ids,
                review_status=review_status,
                created_by=created_by,
                member_routine_ids=member_routine_ids,
            )
        )
        written += 1
    return written


# ---------------------------------------------------------------------------
# R11-FP01: the trigger axis. See the module docstring for why it is here.
# ---------------------------------------------------------------------------

#: How the action-routine join reports a routine it could not read through. The
#: same three words `routine_call_descent` uses for the same three situations, so
#: `footprint_gaps` routes a trigger's unreachable body exactly as it routes a
#: routine's unreachable callee. Restated here rather than imported because
#: `routine_call_descent` imports *this* module -- importing back would be a
#: cycle -- and kept in step by `tests/test_trigger_lineage.py`, which asserts the
#: two modules still agree.
TRIGGER_ROUTINE_NOT_CAPTURED = "NOT_CAPTURED"
TRIGGER_ROUTINE_AMBIGUOUS = "AMBIGUOUS"
TRIGGER_ROUTINE_BODY_WITHHELD = "BODY_WITHHELD"

#: The trigger table's natural key within one trigger. Same shape as
#: `RoutineEdgeKey`; a distinct alias so a signature says which table it keys.
TriggerEdgeKey = tuple[int, str, str, str, str, str, str | None]


@dataclass(frozen=True, slots=True)
class TriggerBody:
    """What one trigger's lineage can be read from, and what it is about.

    `firing_table` is always known -- it is the fact `metadata_trigger` exists to
    carry -- so the subject binding is available even when no body is. `sql` is
    `None` exactly when `unresolved_reason` is set: a PostgreSQL trigger whose
    action routine is not captured here, or whose body that routine withholds.
    """

    #: Schema-qualified firing table, the implicit subject of the body's edges.
    firing_table: str
    #: The redacted body text to parse, or None when none could be reached.
    sql: str | None
    #: The action routine the body came from; None when the trigger carries it.
    routine_id: UUID | None
    #: That routine's qualified name, recorded on each edge it produced.
    via_routine: str | None
    #: Value-free code for why `sql` is None: one of the `TRIGGER_ROUTINE_*` above.
    unresolved_reason: str | None = None


def _qualified_parts(name: str) -> tuple[str | None, str]:
    """`(schema, object)` from a possibly-qualified, possibly-quoted identifier,
    lower-cased. Mirrors `routine_call_descent._name_parts`; see the note on the
    reason codes above for why it is not imported."""
    parts = [part.strip('[]"`').lower() for part in name.split(".")]
    parts = [part for part in parts if part]
    if not parts:
        return None, ""
    return (parts[-2] if len(parts) >= 2 else None), parts[-1]


async def trigger_body(
    session: AsyncSession, datasource: DataSource, trigger: MetadataTrigger
) -> TriggerBody:
    """Resolve what to parse for `trigger`, and the firing table it is about.

    A trigger that carries its own body (SQL Server, Oracle) is gated by
    `require_eligible_trigger_body` and parsed directly. A trigger that does not
    (PostgreSQL, where `availability` is UNAVAILABLE by design and
    `action_routine` names the function holding the code) is joined to that
    routine on the routine axis, matched by name within this datasource and
    organization -- INV-5 on both queries -- and gated by the routine axis's own
    `require_eligible_routine_body`. An overload that matches more than once is
    AMBIGUOUS, never a guess.

    Raises `TriggerNotEligibleError` only for a trigger that does carry a body and
    whose body cannot be used. An unreachable action routine is a gap to record,
    not an error: it comes back as `unresolved_reason`.
    """
    schema_name = await session.scalar(
        select(MetadataSchema.name).where(
            MetadataSchema.organization_id == datasource.organization_id,
            MetadataSchema.id == trigger.schema_id,
        )
    )
    firing_schema = trigger.table_schema_name or schema_name or ""
    firing_table = f"{firing_schema}.{trigger.table_name}" if firing_schema else trigger.table_name

    action_routine = (trigger.action_routine or "").strip()
    if trigger.availability == AVAILABLE or not action_routine:
        # A trigger with neither a body nor a named routine is refused by the gate
        # with the engine's own reason, which is the honest answer.
        return TriggerBody(
            firing_table=firing_table,
            sql=require_eligible_trigger_body(trigger),
            routine_id=None,
            via_routine=None,
        )

    schema, name = _qualified_parts(action_routine)
    rows = (
        await session.execute(
            select(MetadataRoutine, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .where(
                MetadataRoutine.organization_id == datasource.organization_id,
                MetadataRoutine.datasource_id == datasource.id,
                MetadataRoutine.status == "ACTIVE",
            )
        )
    ).all()
    candidates = [
        (routine, routine_schema)
        for routine, routine_schema in rows
        if routine.name.lower() == name
        and (schema is None or routine_schema.lower() == schema)
    ]
    if not candidates:
        return TriggerBody(
            firing_table, None, None, None, unresolved_reason=TRIGGER_ROUTINE_NOT_CAPTURED
        )
    if len(candidates) > 1:
        return TriggerBody(
            firing_table, None, None, None, unresolved_reason=TRIGGER_ROUTINE_AMBIGUOUS
        )
    routine, routine_schema = candidates[0]
    qualified = f"{routine_schema}.{routine.name}"
    try:
        body = require_eligible_routine_body(routine)
    except RoutineNotEligibleError:
        return TriggerBody(
            firing_table,
            None,
            routine.id,
            qualified,
            unresolved_reason=TRIGGER_ROUTINE_BODY_WITHHELD,
        )
    return TriggerBody(firing_table, body, routine.id, qualified)


def unreachable_body_marker(
    body: TriggerBody, *, dialect: str, sql_hash: str
) -> ProcedureParseResult:
    """A parse result that records only that the body could not be reached.

    Never an empty result: a trigger whose code Atlas cannot read is a gap, and a
    zero-edge parse would read as a trigger that touches nothing. The marker
    carries the routine's *name* and a reason code and nothing else -- no body
    text, which there is none of anyway, and no driver message (INV-6).
    """
    reason = (
        f"{UnparsedReason.NESTED_PROCEDURE_CALL.value}: "
        f"{body.via_routine or '<unnamed>'} ({body.unresolved_reason})"
    )
    return unparsed_marker_result(
        reason=reason, dialect=dialect, sql_hash=sql_hash, via_routine=body.via_routine
    )


def trigger_edge_key(
    edge: ProcedureLineageEdgeRecord | TriggerLineageEdge,
) -> TriggerEdgeKey:
    return (
        edge.statement_ordinal,
        edge.source_table,
        edge.source_column,
        edge.target_table,
        edge.target_column,
        edge.transformation_type,
        edge.via_temp_table,
    )


def trigger_edge_row(
    edge: ProcedureLineageEdgeRecord,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    trigger_id: UUID,
    routine_id: UUID | None,
    sql_hash: str,
    table_ids: dict[str, UUID],
    review_status: str,
    created_by: str | None,
) -> TriggerLineageEdge:
    """One parsed trigger edge as a row, its table ids resolved the way a
    routine's are. The body text is not a field here and never becomes one."""
    source_name = persistable_table(edge.source_table, edge.source_resolved)
    target_name = persistable_table(edge.target_table, True)
    row = TriggerLineageEdge(
        organization_id=organization_id,
        datasource_id=datasource_id,
        trigger_id=trigger_id,
        routine_id=routine_id,
        statement_ordinal=edge.statement_ordinal,
        source_table=edge.source_table,
        source_column=edge.source_column,
        target_table=edge.target_table,
        target_column=edge.target_column,
        source_resolved=edge.source_resolved,
        source_table_id=table_ids.get(source_name) if source_name else None,
        target_table_id=table_ids.get(target_name) if target_name else None,
        transformation_type=edge.transformation_type,
        confidence=edge.confidence,
        dialect=edge.dialect,
        is_write=edge.is_write,
        is_intermediate=edge.is_intermediate,
        control_flow_context=edge.control_flow_context,
        unparsed_reason=edge.unparsed_reason,
        via_temp_table=edge.via_temp_table,
        via_routine=edge.via_routine,
        # R11-FP03: a trigger's action routine has no packages of its own, so only
        # the direct, already-resolved id (a cross-routine splice) ever applies here.
        via_routine_id=edge.via_routine_id,
        sql_hash=sql_hash,
        review_status=review_status,
        created_by=created_by,
    )
    # R11-FP07: on PostgreSQL the range indexes the action routine's stored body
    # (`routine_id` names it); on an engine whose trigger carries its own body, the
    # trigger's. The digest says which text either way.
    apply_statement_range(row, edge)
    return row


async def persist_trigger_edges(
    session: AsyncSession,
    *,
    datasource: DataSource,
    trigger: MetadataTrigger,
    result: ProcedureParseResult,
    review_mode: str,
    threshold: float,
    created_by: str | None,
    routine_id: UUID | None = None,
    agent_proposal: bool = False,
) -> list[TriggerLineageEdge]:
    """One trigger's parse, written under ADR-0026's review mode. Returns the rows
    written, so a caller can report their ids without re-reading the table.

    The same rule `persist_routine_edges` applies, for the same reason: only
    ACTIVE edges steer retrieval and tool generation, so an edge nobody has
    decided must not arrive ACTIVE. `require_review` lands each edge through
    `resolve_review_status_for_new_edge` and a re-parse replaces only what is
    undecided; an UNPARSED marker is a gap rather than an edge, so it is never put
    in front of a reviewer and is ACTIVE in either mode. Every statement restates
    `organization_id` and `datasource_id` (INV-5).

    `agent_proposal` lands every real edge PROPOSED whatever the review mode or
    the auto-activation threshold say (ADR-0029): those settings govern what a
    *person's* parse may activate, and an agent's output is decided by a person in
    the per-edge queue. The markers stay ACTIVE, because a gap is not a proposal.

    Under `require_review` the returned rows also include decided edges this parse
    reconciled (`reconcile_decided_edges`): SUPERSEDED ones and ones revived to
    PROPOSED, so a caller can report both without re-reading the table.
    """
    clear = delete(TriggerLineageEdge).where(
        TriggerLineageEdge.organization_id == datasource.organization_id,
        TriggerLineageEdge.datasource_id == datasource.id,
        TriggerLineageEdge.trigger_id == trigger.id,
    )
    if review_mode == "require_review":
        clear = clear.where(
            or_(
                TriggerLineageEdge.review_status == "PROPOSED",
                TriggerLineageEdge.transformation_type == UNPARSED_TRANSFORMATION_TYPE,
            )
        )
    await session.execute(clear)
    reconciliation = DecidedEdgeReconciliation()
    if review_mode == "require_review":
        # As `persist_routine_edges`: decided edges follow the body (R11-FP01).
        reconciliation = await reconcile_decided_edges(
            session,
            model=TriggerLineageEdge,
            datasource=datasource,
            owner_id=trigger.id,
            result=result,
            produced=result.edges,
        )
    changed = [*reconciliation.superseded, *reconciliation.revived]
    if not result.edges:
        return changed

    kept: set[TriggerEdgeKey] = set()
    if review_mode == "require_review":
        kept = {
            trigger_edge_key(row)
            for row in (
                await session.scalars(
                    select(TriggerLineageEdge).where(
                        TriggerLineageEdge.organization_id == datasource.organization_id,
                        TriggerLineageEdge.datasource_id == datasource.id,
                        TriggerLineageEdge.trigger_id == trigger.id,
                    )
                )
            ).all()
        }
    table_ids = await resolve_routine_table_ids(session, datasource.id, result.edges)
    written: list[TriggerLineageEdge] = []
    for edge in result.edges:
        key = trigger_edge_key(edge)
        if key in kept:
            continue
        kept.add(key)
        if edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE:
            review_status = "ACTIVE"
        elif agent_proposal:
            review_status = "PROPOSED"
        else:
            review_status = resolve_review_status_for_new_edge(
                review_mode=review_mode,
                confidence=edge.confidence,
                threshold=threshold,
                source_trusted=None,  # a captured body is parsed here, never pushed
            )
        row = trigger_edge_row(
            edge,
            organization_id=datasource.organization_id,
            datasource_id=datasource.id,
            trigger_id=trigger.id,
            routine_id=routine_id,
            sql_hash=result.sql_hash,
            table_ids=table_ids,
            review_status=review_status,
            created_by=created_by,
        )
        session.add(row)
        written.append(row)
    return [*written, *changed]


async def record_trigger_parse_coverage(
    session: AsyncSession,
    *,
    datasource: DataSource,
    trigger: MetadataTrigger,
    result: ProcedureParseResult,
    routine_id: UUID | None,
    measured_by: str | None,
) -> TriggerParseCoverage:
    """Store how completely `trigger`'s body was understood by this parse.

    `record_routine_parse_coverage` on the trigger axis, with the same one-row-
    per-object, replaced-in-place rule. `routine_id` is the routine whose body was
    actually read (`TriggerBody.routine_id`): the join a later change to that
    routine's body uses to find this trigger again. Recorded for a body that could
    not be reached too, from its marker result, so "not reachable" is a
    measurement rather than an absence. Both lookups restate the organization
    and the datasource (INV-5).
    """
    existing = (
        await session.scalars(
            select(TriggerParseCoverage).where(
                TriggerParseCoverage.organization_id == datasource.organization_id,
                TriggerParseCoverage.datasource_id == datasource.id,
                TriggerParseCoverage.trigger_id == trigger.id,
            )
        )
    ).first()
    row = existing or TriggerParseCoverage(
        organization_id=datasource.organization_id,
        datasource_id=datasource.id,
        trigger_id=trigger.id,
    )
    row.routine_id = routine_id
    _measure(row, result, measured_by=measured_by)
    if existing is None:
        session.add(row)
    return row
