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

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.envelope_models import MetadataRoutine
from aida.models import DataSource, MetadataSchema
from aida.procedure_lineage import (
    PROCEDURE_LOCAL_TARGET,
    UNPARSED_TRANSFORMATION_TYPE,
    ProcedureLineageEdgeRecord,
    ProcedureParseResult,
    StatementRangeStatus,
    UnparsedReason,
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
    return replace(
        edge,
        statement_ordinal=call.statement_ordinal,
        confidence=confidence,
        target_table=target,
        is_intermediate=intermediate,
        is_write=write,
        control_flow_context=call.control_flow_context or edge.control_flow_context,
        via_routine=callee,
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
        if missing is None and (callee.key is None or callee.body is None):
            missing = CALLEE_NOT_CAPTURED
        elif missing is None and callee.key in path:
            missing = CALLEE_CYCLE
        elif missing is None and depth >= MAX_DEPTH:
            missing = CALLEE_DEPTH_LIMIT
        elif missing is None and budget.remaining <= 0:
            missing = CALLEE_LIMIT
        if missing is None and callee.key is not None and callee.body is not None:
            budget.remaining -= 1
            child = _descend(
                parse_procedure_lineage(callee.body, dialect=dialect),
                dialect=dialect,
                resolve=resolve,
                path=path | {callee.key},
                depth=depth + 1,
                budget=budget,
            )
            edges.extend(
                _at_call_site(child_edge, edge, callee.qualified_name or name, dialect, kind, name)
                for child_edge in child.edges
                if child_edge.transformation_type != UNPARSED_TRANSFORMATION_TYPE
            )
            if child.is_fully_parsed:
                continue
            missing = CALLEE_NOT_FULLY_PARSED
        edges.append(replace(edge, unparsed_reason=f"{_PREFIXES[kind]}{name} ({missing})"))
    return _summarised(result, _deduplicated(edges))


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
    # A table function's rows arrive as an intermediate, so the hops through it are the
    # caller's own end-to-end lineage.
    spliced = [*descended.edges, *propagate_intermediate_hops(descended.edges)]
    return _summarised(descended, _deduplicated(spliced))


def _name_parts(name: str) -> tuple[str | None, str]:
    parts = [part.strip('[]"`').lower() for part in name.split(".")]
    parts = [part for part in parts if part]
    if not parts:
        return None, ""
    return (parts[-2] if len(parts) >= 2 else None), parts[-1]


async def routine_resolver(
    session: AsyncSession, datasource: DataSource, caller: MetadataRoutine
) -> Resolver:
    """Resolve a called routine's name among the ACTIVE routines captured in `datasource`."""
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
    for routine, schema_name in rows:
        entry = (routine, schema_name)
        by_qualified.setdefault((schema_name.lower(), routine.name.lower()), []).append(entry)
        by_name.setdefault(routine.name.lower(), []).append(entry)

    def resolve(name: str) -> Callee:
        schema, routine_name = _name_parts(name)
        if schema is not None:
            candidates = by_qualified.get((schema, routine_name), [])
        else:
            candidates = (
                by_qualified.get((caller_schema, routine_name), []) if caller_schema else []
            ) or by_name.get(routine_name, [])
        if not candidates:
            return Callee(None, None, None, CALLEE_NOT_CAPTURED)
        if len(candidates) > 1:
            return Callee(None, None, None, CALLEE_AMBIGUOUS)
        routine, schema_name = candidates[0]
        qualified = f"{schema_name}.{routine.name}"
        try:
            body = require_eligible_routine_body(routine)
        except RoutineNotEligibleError:
            return Callee(str(routine.id), qualified, None, CALLEE_BODY_WITHHELD)
        return Callee(str(routine.id), qualified, body)

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
