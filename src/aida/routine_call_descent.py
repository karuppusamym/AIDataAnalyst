"""R11-FP07: bounded descent into the routines a routine calls.

`procedure_lineage` marks `CALL p()`, `EXEC p` and `PERFORM f()` as a NESTED_PROCEDURE_CALL gap,
and a `FROM s.fn(...)` source as a TABLE_FUNCTION_READ gap: the caller's own body says nothing
about what the callee reads or writes. When the callee is a routine Atlas has captured in the
same datasource, that lineage is known, and this reads it.

* **Resolution** is by name within the caller's datasource. A qualified name matches its schema;
  a bare name matches the caller's schema first, then a name unique across the datasource. More
  than one match (a PostgreSQL overload) is AMBIGUOUS, never a guess. A callee's body passes the
  same gate as any captured body (`require_eligible_routine_body`).
* **Descent** parses the callee and, recursively, its own callees: at most `MAX_DEPTH` levels and
  `MAX_CALLEES` bodies per root, and never a routine already on the call path (a cycle).
* **Edges** read from a callee become the caller's, at the call's statement ordinal, carrying the
  called routine's qualified name in `via_routine`, and at most PARTIAL confidence: a parameter
  can steer the callee's branches. On PostgreSQL a callee's result set stays inside the caller
  (`PERFORM` discards it and `CALL` returns none), so it becomes a local read; on SQL Server `EXEC`
  streams it into the caller's own result. A table function's rows land on the name the caller
  selects from, so the hop propagation `procedure_lineage` already runs joins the function's own
  sources to whatever the caller writes.
* **The gap marker** goes only when the callee was fully parsed, all the way down. Otherwise it
  stays and names why: NOT_CAPTURED, AMBIGUOUS, BODY_WITHHELD, CYCLE, DEPTH_LIMIT, CALLEE_LIMIT or
  CALLEE_NOT_FULLY_PARSED. A gap that already names its outcome is left as it is: the parser
  writes one for a call between members of one Oracle package, which it read through itself
  (R11-FP03).

Only lineage uses this. Tool generation still refuses a routine with a nested call: reading a call
through proves what it touches, not that invoking it is safe. Bodies are the stored value-free text,
and nothing is executed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import MetadataRoutine
from aida.models import DataSource, MetadataSchema
from aida.procedure_lineage import (
    MEMBER_CALL_NOT_FULLY_PARSED,
    PROCEDURE_LOCAL_TARGET,
    UNPARSED_TRANSFORMATION_TYPE,
    PendingMemberCall,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    StatementRangeStatus,
    UnparsedReason,
    owned_loop_record,
    parse_procedure_lineage,
    propagate_intermediate_hops,
)
from aida.routine_lineage_edges import RoutineNotEligibleError, require_eligible_routine_body
from aida.sql_lineage_parser import PROCEDURE_RESULT_TARGET, Confidence

MAX_DEPTH: Final = 3
MAX_CALLEES: Final = 25

CALLEE_NOT_CAPTURED: Final = "NOT_CAPTURED"
CALLEE_AMBIGUOUS: Final = "AMBIGUOUS"
CALLEE_BODY_WITHHELD: Final = "BODY_WITHHELD"
CALLEE_CYCLE: Final = "CYCLE"
CALLEE_DEPTH_LIMIT: Final = "DEPTH_LIMIT"
CALLEE_LIMIT: Final = "CALLEE_LIMIT"
CALLEE_NOT_FULLY_PARSED: Final = "CALLEE_NOT_FULLY_PARSED"

_RANK: Final = {Confidence.LOW.value: 0, Confidence.PARTIAL.value: 1, Confidence.FULL.value: 2}
#: Dialects whose nested call streams a callee's result set into the caller's own result.
_RESULT_STREAMING_DIALECTS: Final = frozenset({"tsql"})


@dataclass(frozen=True, slots=True)
class Callee:
    """What a call resolves to: an identity and its body, or why there is none."""

    key: str | None
    qualified_name: str | None
    body: str | None
    missing: str | None = None
    # R11-FP03: the callee's own captured routine id, when the resolver already
    # knows it (a real catalog row) -- carried onto the spliced edges' own
    # `via_routine_id` so a reader is never left re-deriving it from `via_routine`'s
    # display text. `None` for a resolver (tests, mainly) that has no such catalog.
    routine_id: UUID | None = None
    # R11-FP03: set instead of `body` when the resolver has already produced the
    # callee's parse itself, filtered to less than the whole thing -- a cross-package
    # member call (`_cross_package_resolver`), whose "body" is one member's own slice
    # of another package's parse, not text this module could hand back to be parsed
    # again without re-admitting the rest of that package. `_descend` recurses into
    # it exactly as it would a fresh `parse_procedure_lineage` of `body`.
    parsed: ProcedureParseResult | None = None


Resolver = Callable[[str], Callee]


@dataclass(slots=True)
class _Budget:
    remaining: int


KIND_CALL: Final = "CALL"
KIND_TABLE_FUNCTION: Final = "TABLE_FUNCTION"
_PREFIXES: Final = {
    KIND_CALL: f"{UnparsedReason.NESTED_PROCEDURE_CALL.value}: ",
    KIND_TABLE_FUNCTION: f"{UnparsedReason.TABLE_FUNCTION_READ.value}: ",
}
#: R11-FP03: a gap whose outcome is already written after the name -- `name (CODE)`.
#: The parser writes one for a call between members of one package, which it resolved
#: against that package's own members. Resolving the name again here, against the
#: schema's routines, would ignore PL/SQL's scoping (a member shadows a schema-level
#: routine of the same name) and would find the member's routine, which is captured
#: with no body of its own -- BODY_WITHHELD, for a body that was right there and read.
_DECIDED_RE: Final = re.compile(r"\s\([A-Z_]+\)$")


def called_routine(edge: ProcedureLineageEdgeRecord) -> tuple[str, str] | None:
    """The routine a gap names and how it is read -- called, or read as a table function."""
    reason = edge.unparsed_reason
    if edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE or not reason:
        return None
    for kind, prefix in _PREFIXES.items():
        if reason.startswith(prefix):
            name = reason[len(prefix) :].split(" (", 1)[0].strip()
            return (kind, name) if name else None
    return None


def callee_name(edge: ProcedureLineageEdgeRecord) -> str | None:
    """The routine a gap names, however it is read."""
    called = called_routine(edge)
    return called[1] if called is not None else None


def _at_call_site(
    edge: ProcedureLineageEdgeRecord,
    call: ProcedureLineageEdgeRecord,
    callee: str,
    dialect: str,
    kind: str,
    source_name: str,
    *,
    via_routine_id: UUID | None = None,
    via_routine_locator: int | None = None,
) -> ProcedureLineageEdgeRecord:
    confidence = (
        edge.confidence
        if _RANK.get(edge.confidence, 0) < _RANK[Confidence.PARTIAL.value]
        else Confidence.PARTIAL.value
    )
    target, intermediate, write = edge.target_table, edge.is_intermediate, edge.is_write
    if target == PROCEDURE_RESULT_TARGET:
        if kind == KIND_TABLE_FUNCTION:
            # The function's rows are what the caller selects from, under the name it reads.
            target, intermediate, write = source_name, True, False
        elif dialect not in _RESULT_STREAMING_DIALECTS:
            target, intermediate, write = PROCEDURE_LOCAL_TARGET, True, False
    # R11-FP07: the callee's own range indexes the callee's body, a different text
    # from the caller's -- keeping it would point a reader at the right offsets in the
    # wrong routine. The edge is the caller's at the call, so it is located at the
    # call (CALL_SITE), in the caller's text; a call that was not located stays
    # NOT_LOCATED. R11-FP03: and it belongs to whichever package member made the call.
    # 2026-09-19: a loop record's name is unique in one body only; carried into the caller
    # it takes the callee's name, so the hop pass run over both cannot join two loops.
    return replace(
        edge,
        source_table=owned_loop_record(edge.source_table, callee) or edge.source_table,
        via_temp_table=owned_loop_record(edge.via_temp_table, callee),
        statement_ordinal=call.statement_ordinal,
        confidence=confidence,
        target_table=owned_loop_record(target, callee) or target,
        is_intermediate=intermediate,
        is_write=write,
        control_flow_context=call.control_flow_context or edge.control_flow_context,
        via_routine=callee,
        via_routine_id=via_routine_id,
        via_routine_locator=via_routine_locator,
        statement_range=call.statement_range,
        statement_range_status=(
            StatementRangeStatus.CALL_SITE.value
            if call.statement_range is not None
            else StatementRangeStatus.NOT_LOCATED.value
        ),
        statement_text_digest=call.statement_text_digest,
        # Token grain likewise: the callee's tokens index the callee's body, and the
        # call names neither end of the edge, so both are unlocated here.
        source_token_range=None,
        target_token_range=None,
        package_member=call.package_member,
        member_attribution=call.member_attribution,
    )


def _deduplicated(edges: Iterable[ProcedureLineageEdgeRecord]) -> list[ProcedureLineageEdgeRecord]:
    """First edge per stored natural key (`deep_procedure_lineage_edge`'s unique constraint)."""
    seen: set[tuple[object, ...]] = set()
    kept: list[ProcedureLineageEdgeRecord] = []
    for edge in edges:
        key = (
            edge.statement_ordinal,
            edge.source_table,
            edge.source_column,
            edge.target_table,
            edge.target_column,
            edge.transformation_type,
            edge.via_temp_table,
        )
        if key not in seen:
            seen.add(key)
            kept.append(edge)
    return kept


def _summarised(
    result: ProcedureParseResult, edges: list[ProcedureLineageEdgeRecord]
) -> ProcedureParseResult:
    gaps = any(edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE for edge in edges)
    real = [edge for edge in edges if edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE]
    fully_parsed = not gaps
    if not fully_parsed:
        confidence = Confidence.PARTIAL.value if real else Confidence.LOW.value
    elif real:
        confidence = (
            Confidence.FULL.value
            if all(edge.confidence == Confidence.FULL.value for edge in real)
            else Confidence.PARTIAL.value
        )
    else:
        confidence = result.confidence
    return ProcedureParseResult(
        edges=edges,
        statement_count=result.statement_count,
        confidence=confidence,
        dialect=result.dialect,
        sql_hash=result.sql_hash,
        errors=list(result.errors),
        is_fully_parsed=fully_parsed,
        is_read_only=(
            fully_parsed and result.statement_count > 0 and not any(edge.is_write for edge in edges)
        ),
        # R11-FP07/FP03: descent adds edges to the caller's parse; which text its
        # ranges index and how its package was attributed are the caller's still.
        statement_text_digest=result.statement_text_digest,
        member_attribution=result.member_attribution,
        member_fallback_reason=result.member_fallback_reason,
        package_members=result.package_members,
        pending_member_calls=result.pending_member_calls,
    )


def _descend(
    result: ProcedureParseResult,
    *,
    dialect: str,
    resolve: Resolver,
    path: frozenset[str],
    depth: int,
    budget: _Budget,
) -> ProcedureParseResult:
    edges: list[ProcedureLineageEdgeRecord] = []
    for edge in result.edges:
        called = called_routine(edge)
        if called is None or _DECIDED_RE.search(edge.unparsed_reason or ""):
            edges.append(edge)
            continue
        kind, name = called
        callee = resolve(name)
        missing = callee.missing
        has_content = callee.body is not None or callee.parsed is not None
        if missing is None and (callee.key is None or not has_content):
            missing = CALLEE_NOT_CAPTURED
        elif missing is None and callee.key in path:
            missing = CALLEE_CYCLE
        elif missing is None and depth >= MAX_DEPTH:
            missing = CALLEE_DEPTH_LIMIT
        elif missing is None and budget.remaining <= 0:
            missing = CALLEE_LIMIT
        if missing is None and callee.key is not None and has_content:
            budget.remaining -= 1
            if callee.parsed is not None:
                starting_point = callee.parsed
            else:
                assert callee.body is not None  # `has_content` guarantees one of the two
                starting_point = parse_procedure_lineage(callee.body, dialect=dialect)
            child = _descend(
                starting_point,
                dialect=dialect,
                resolve=resolve,
                path=path | {callee.key},
                depth=depth + 1,
                budget=budget,
            )
            edges.extend(
                _at_call_site(
                    child_edge,
                    edge,
                    callee.qualified_name or name,
                    dialect,
                    kind,
                    name,
                    via_routine_id=callee.routine_id,
                )
                for child_edge in child.edges
                if child_edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE
            )
            if child.is_fully_parsed:
                continue
            missing = CALLEE_NOT_FULLY_PARSED
        edges.append(replace(edge, unparsed_reason=f"{_PREFIXES[kind]}{name} ({missing})"))
    return _summarised(result, _deduplicated(edges))


def _member_edges(
    result: ProcedureParseResult, member_index: int
) -> list[ProcedureLineageEdgeRecord]:
    """Every edge belonging to one package member's own `[first_ordinal,
    last_ordinal]` span (`ProcedureParseResult.package_members`) -- its statements'
    own facts, whatever its in-package sibling calls had already spliced in at
    parse time, and any gap that is still unresolved. Never the rest of the
    package. Used both to re-check a sibling call this module deferred (R11-FP03's
    ordering fix, below) and to read one named member out of a *different*
    package's own parse for a cross-package call (`_cross_package_resolver`)."""
    if member_index >= len(result.package_members):
        return []
    member = result.package_members[member_index]
    if member.first_ordinal is None or member.last_ordinal is None:
        return []
    return [
        edge
        for edge in result.edges
        if member.first_ordinal <= edge.statement_ordinal <= member.last_ordinal
    ]


def _member_parse_result(
    result: ProcedureParseResult, member_index: int
) -> ProcedureParseResult:
    """One package member's own edges, read as if that member alone had been
    parsed -- `_descend`'s recursive step treats this exactly like a fresh
    `parse_procedure_lineage` of some callee's body, so a cross-package call
    (`_cross_package_resolver`) reads through the *member*, never the rest of
    the package it lives in."""
    edges = _member_edges(result, member_index)
    real = [edge for edge in edges if edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE]
    fully_parsed = len(real) == len(edges)
    return ProcedureParseResult(
        edges=edges,
        statement_count=len(edges) or 1,
        confidence=(
            Confidence.FULL.value
            if fully_parsed and real and all(e.confidence == Confidence.FULL.value for e in real)
            else Confidence.PARTIAL.value if real or not fully_parsed else Confidence.LOW.value
        ),
        dialect=result.dialect,
        sql_hash=result.sql_hash,
        errors=[e.unparsed_reason for e in edges if e.unparsed_reason],
        is_fully_parsed=fully_parsed,
        is_read_only=fully_parsed and not any(e.is_write for e in real),
        statement_text_digest=result.statement_text_digest,
    )


def _reconcile_pass(
    result: ProcedureParseResult, pending: tuple[PendingMemberCall, ...]
) -> tuple[ProcedureParseResult, bool]:
    """One fixed-point step of `_reconcile_pending_member_calls`: every pending call
    whose reached members are, as of `result`'s *current* edges, no longer blocked
    gets its final splice; everything else, including a pending call still blocked
    only because another pending call has not resolved yet, is left as it is for
    the next step to re-check against this step's own progress."""
    pending_by_ordinal = {item.statement_ordinal: item for item in pending}
    edges: list[ProcedureLineageEdgeRecord] = []
    changed = False
    for edge in result.edges:
        item = pending_by_ordinal.get(edge.statement_ordinal)
        is_candidate = (
            item is not None
            and edge.transformation_type == UNPARSED_TRANSFORMATION_TYPE
            and (edge.unparsed_reason or "").endswith(f"({MEMBER_CALL_NOT_FULLY_PARSED})")
        )
        if not is_candidate:
            edges.append(edge)
            continue
        assert item is not None  # narrows for mypy; `is_candidate` already checked it
        reached_edges = {
            index: _member_edges(result, index) for index in item.reached_member_indices
        }
        blocked = any(
            any(e.transformation_type == UNPARSED_TRANSFORMATION_TYPE for e in group)
            for group in reached_edges.values()
        )
        if blocked:
            edges.append(edge)
            continue
        changed = True
        target_index = item.reached_member_indices[0] if item.reached_member_indices else None
        via_ordinal = (
            result.package_members[target_index].first_ordinal
            if target_index is not None and target_index < len(result.package_members)
            else None
        )
        edges.extend(
            _at_call_site(
                reached_edge,
                edge,
                item.via_routine,
                result.dialect,
                KIND_CALL,
                item.via_routine,
                via_routine_locator=via_ordinal,
            )
            for group in reached_edges.values()
            for reached_edge in group
            if reached_edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE
        )
    if not changed:
        return result, False
    return _summarised(result, _deduplicated(edges)), True


def _reconcile_pending_member_calls(result: ProcedureParseResult) -> ProcedureParseResult:
    """R11-FP03: a sibling-member call's completeness that `_member_calls_read_through`
    deferred (`PendingMemberCall`) because a member it reached still had its own
    external call, decided now that the descent pass above has resolved every
    ordinary gap in this same parse.

    A member still blocked -- its own call stayed a gap, or resolved to a further
    decided gap (AMBIGUOUS, NOT_CAPTURED, ...) -- keeps today's `CALLEE_NOT_FULLY_
    PARSED` marker, unchanged. One that is now clear gets its reached members'
    edges spliced in, exactly as an eager in-package resolution would have.

    **Bounded, not unlimited**: run to a fixed point, at most `len(pending) + 1`
    passes -- enough for a chain of *pending* calls of any depth within this one
    package (each pass resolves at least one more link, since a chain longer than
    the pass count would mean nothing changed and the loop already stopped) to
    settle in the order their own blockers clear, not the order they were written
    in. What it does **not** reach: a *callee* that is itself a package with its
    own unresolved sibling-call ordering issue -- `_descend`'s recursive call into
    such a callee never runs this reconciliation on the callee's own parse, so the
    callee is conservatively read as not fully parsed, the same as today, rather
    than potentially resolving further.
    """
    pending = result.pending_member_calls
    if not pending:
        return result
    current = result
    for _ in range(len(pending) + 1):
        current, changed = _reconcile_pass(current, pending)
        if not changed:
            break
    return replace(current, pending_member_calls=())


def descend_nested_calls(
    result: ProcedureParseResult, *, dialect: str, resolve: Resolver, root_key: str
) -> ProcedureParseResult:
    """`result` with each nested call read through where its callee resolves; see the module."""
    if not any(called_routine(edge) for edge in result.edges):
        return result
    descended = _descend(
        result,
        dialect=dialect,
        resolve=resolve,
        path=frozenset({root_key}),
        depth=0,
        budget=_Budget(MAX_CALLEES),
    )
    reconciled = _reconcile_pending_member_calls(descended)
    # A table function's rows arrive as an intermediate, so the hops through it are the
    # caller's own end-to-end lineage.
    spliced = [*reconciled.edges, *propagate_intermediate_hops(reconciled.edges)]
    return _summarised(reconciled, _deduplicated(spliced))


def _name_parts(name: str) -> tuple[str | None, str]:
    parts = [part.strip('[]"`').lower() for part in name.split(".")]
    parts = [part for part in parts if part]
    if not parts:
        return None, ""
    return (parts[-2] if len(parts) >= 2 else None), parts[-1]


async def routine_resolver(
    session: AsyncSession, datasource: DataSource, caller: MetadataRoutine
) -> Resolver:
    """Resolve a called routine's name among the ACTIVE routines captured in `datasource`.

    R11-FP03: when no routine matches the name at all, and the name has at least
    one qualifier, the qualifier may instead be a *package* in the caller's own
    schema and the rest one of its members -- `other_pkg.member(...)`, a call this
    parser leaves for descent because it is not the caller's own package (an
    in-package sibling call is already read through before descent ever runs).
    Narrower than the in-package case on purpose: only a package in the caller's
    own schema, found by name; a fully schema-qualified `schema.pkg.member` is not
    read (`_name_parts` keeps only the last two dotted parts, the same limit a
    plain two-part schema-qualified routine call already has here). An overloaded
    member name (more than one member of that package sharing it) is AMBIGUOUS,
    never a guess -- the same rule the in-package resolver uses, and, like it, by
    name only: this gap marker never carried the call's arguments.
    """
    rows = (
        await session.execute(
            select(MetadataRoutine, MetadataSchema.name)
            .join(MetadataSchema, MetadataSchema.id == MetadataRoutine.schema_id)
            .where(
                MetadataRoutine.datasource_id == datasource.id,
                MetadataRoutine.organization_id == datasource.organization_id,
                MetadataRoutine.status == "ACTIVE",
            )
        )
    ).all()
    caller_schema = next(
        (schema_name.lower() for routine, schema_name in rows if routine.id == caller.id), None
    )
    by_qualified: dict[tuple[str, str], list[tuple[MetadataRoutine, str]]] = {}
    by_name: dict[str, list[tuple[MetadataRoutine, str]]] = {}
    by_package: dict[tuple[str, str], list[tuple[MetadataRoutine, str]]] = {}
    for routine, schema_name in rows:
        entry = (routine, schema_name)
        by_qualified.setdefault((schema_name.lower(), routine.name.lower()), []).append(entry)
        by_name.setdefault(routine.name.lower(), []).append(entry)
        if routine.routine_type.strip().upper() == "PACKAGE":
            by_package.setdefault((schema_name.lower(), routine.name.lower()), []).append(entry)
    #: Cache: a package resolved cross-package is parsed once even if several
    #: calls (or several members) reach it.
    package_parses: dict[UUID, ProcedureParseResult] = {}

    def cross_package_member(package_name: str, member_name: str) -> Callee:
        packages = by_package.get((caller_schema or "", package_name), [])
        if not packages:
            return Callee(None, None, None, CALLEE_NOT_CAPTURED)
        if len(packages) > 1:
            return Callee(None, None, None, CALLEE_AMBIGUOUS)
        package_routine, schema_name = packages[0]
        try:
            package_body = require_eligible_routine_body(package_routine)
        except RoutineNotEligibleError:
            qualified = f"{schema_name}.{package_routine.name}"
            return Callee(str(package_routine.id), qualified, None, CALLEE_BODY_WITHHELD)
        pkg_result = package_parses.get(package_routine.id)
        if pkg_result is None:
            pkg_result = parse_procedure_lineage(package_body, dialect=datasource.dialect)
            package_parses[package_routine.id] = pkg_result
        matches = [
            index
            for index, member in enumerate(pkg_result.package_members)
            if member.name.lower() == member_name
        ]
        if not matches:
            return Callee(None, None, None, CALLEE_NOT_CAPTURED)
        if len(matches) > 1:
            return Callee(None, None, None, CALLEE_AMBIGUOUS)
        member = pkg_result.package_members[matches[0]]
        qualified = f"{schema_name}.{package_routine.name}.{member.name}"
        key = f"{package_routine.id}:{matches[0]}"
        return Callee(
            key, qualified, None, parsed=_member_parse_result(pkg_result, matches[0])
        )

    def resolve(name: str) -> Callee:
        schema, routine_name = _name_parts(name)
        if schema is not None:
            candidates = by_qualified.get((schema, routine_name), [])
        else:
            candidates = (
                by_qualified.get((caller_schema, routine_name), []) if caller_schema else []
            ) or by_name.get(routine_name, [])
        if candidates:
            if len(candidates) > 1:
                return Callee(None, None, None, CALLEE_AMBIGUOUS)
            routine, schema_name = candidates[0]
            qualified = f"{schema_name}.{routine.name}"
            try:
                body = require_eligible_routine_body(routine)
            except RoutineNotEligibleError:
                return Callee(str(routine.id), qualified, None, CALLEE_BODY_WITHHELD)
            return Callee(str(routine.id), qualified, body, routine_id=routine.id)
        if schema is not None:
            return cross_package_member(schema, routine_name)
        return Callee(None, None, None, CALLEE_NOT_CAPTURED)

    return resolve


async def descend_routine_calls(
    session: AsyncSession,
    datasource: DataSource,
    routine: MetadataRoutine,
    result: ProcedureParseResult,
) -> ProcedureParseResult:
    """A captured routine's parse, with its calls to routines captured here read through."""
    if not any(called_routine(edge) for edge in result.edges):
        return result
    resolve = await routine_resolver(session, datasource, routine)
    return descend_nested_calls(
        result, dialect=datasource.dialect, resolve=resolve, root_key=str(routine.id)
    )
