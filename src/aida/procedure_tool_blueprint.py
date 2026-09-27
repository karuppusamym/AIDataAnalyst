"""N12: deterministic procedure-to-tool ("tool generator C") blueprint
generation, gated on N3's read-only proof.

**Why the generated SQL is the procedure's own final result statement, not a
fresh query against its source tables.** `view_tool_blueprint.py` (N11)
deliberately does not inline a view's SQL -- it builds `SELECT <columns>
FROM <view>`, because the view itself stays the single source of truth for
its own JOIN/aggregation logic and is always queryable live. A stored
procedure has no such live, callable, column-typed surface: its output is
whatever its final SELECT computes, not a catalog object with its own
`MetadataColumn` rows. So this generator's SQL template *is* that final
SELECT, reconstructed from the parsed AST of the routine's own (already
literal-redacted) body -- the only text this platform ever has for a
procedure (INV-6; the raw body, literals included, is never persisted
anywhere, by envelope 1.1 design, see `envelope_models.py`).

**Why a literal anywhere in the result statement refuses generation
outright.** A redacted literal (`'<REDACTED>'`, `<NUM>`) in a WHERE clause
is not recoverable -- it is not "the real value, just hidden", it is gone.
Reconstructing `WHERE status = '<REDACTED>'` as an executable tool would
silently return wrong (usually empty) results while looking like a normal,
working tool. `_reject_if_literal_present` refuses generation the moment any
`exp.Literal` survives in the reconstructed statement, the same
refuse-rather-than-guess posture `view_tool_blueprint.py` applies to a
missing/unparsed view definition.

**Procedure IN parameters become tool parameters, or generation is
refused.** A bound variable reference (`@start_date`, `:end_date`) in the
result statement is not a literal -- it never depended on redacted source
text -- so it is not inherently a problem. Each one is matched by name
(case-insensitive) against the routine's own declared `IN`/`INOUT`
`MetadataRoutineParameter` rows and, when its physical type maps to a known
filterable family, rewritten to an `exp.Placeholder` and exposed as a real
`ToolParameterDefinition` -- exactly `view_tool_blueprint.py`'s "safe
default" philosophy for an unmappable *column*, but a variable reference
gets the stricter treatment a column does not: a variable this generator
cannot map is refused outright (never left in the SQL as inert, unbound
text -- that would pass validation only to fail, or silently misbehave, the
first time the tool actually ran), where an unfilterable *column* in
`view_tool_blueprint.py` is merely left unexposed as a parameter. The two
differ because a column that is not offered as a filter is still valid SQL;
a variable reference the renderer does not resolve to a placeholder is not.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final
from uuid import UUID

from sqlalchemy import select

from aida.envelope_models import MetadataRoutine, MetadataRoutineParameter
from aida.procedure_lineage import (
    ParsedStatement,
    ProcedureParseResult,
    parse_procedure_lineage,
    walk_procedure_statements,
)
from aida.relationship_naming import physical_type_family
from aida.routine_lineage_edges import RoutineNotEligibleError, require_eligible_routine_body
from aida.schemas import ToolParameterDefinition
from aida.sql_lineage_parser import PROCEDURE_RESULT_TARGET
from aida.view_tool_blueprint import _PARAMETER_TYPE_BY_PHYSICAL_FAMILY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlglot import exp

try:
    from sqlglot import exp as _exp
    from sqlglot import parse_one as _parse_one
    from sqlglot.errors import ParseError as _ParseError
    from sqlglot.errors import TokenError as _TokenError
    from sqlglot.optimizer.normalize_identifiers import (
        normalize_identifiers as _normalize_identifiers,
    )

    _SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SQLGLOT_AVAILABLE = False


class ProcedureToolBlueprintError(ValueError):
    """Raised for a structurally invalid blueprint request."""


class ProcedureNotEligibleError(ProcedureToolBlueprintError):
    """The routine failed N12's eligibility gate: not read-only-provable,
    or its single result statement cannot be safely reconstructed into an
    executable tool. Always names the specific reason -- never guessed."""

    def __init__(self, reason: str, *, code: str = "PROCEDURE_NOT_ELIGIBLE") -> None:
        self.reason = reason
        #: Stable and value-free, for a caller that records why rather than says it (R11-FP14).
        self.code = code
        super().__init__(f"procedure is not eligible for tool generation: {reason}")


@dataclass(frozen=True, slots=True)
class RoutineInParameter:
    name: str
    physical_type: str


@dataclass(frozen=True, slots=True)
class ProcedureToolBlueprint:
    """The deterministic render output -- ready to drop into
    `GovernedToolVersionCreate.sql_template` / `.parameters`."""

    sql_template: str
    parameters: tuple[ToolParameterDefinition, ...]
    referenced_tables: tuple[str, ...]
    statement_count: int
    sql_hash: str


def _require_no_literals(node: exp.Expr) -> None:
    literal = node.find(_exp.Literal)
    if literal is not None:
        raise ProcedureNotEligibleError(
            "the procedure's result statement contains a literal value "
            "that cannot be safely reconstructed from redacted source text "
            f"({literal.sql()!r}) -- expose the underlying tables via the "
            "view/multi-table tool generator instead, or rewrite the "
            "procedure to take the value as a parameter",
            code="PROCEDURE_RESULT_HAS_LITERAL",
        )


def _remap_parameters(
    node: exp.Expr, routine_parameters: Sequence[RoutineInParameter]
) -> tuple[exp.Expr, list[ToolParameterDefinition]]:
    by_name = {parameter.name.lower(): parameter for parameter in routine_parameters}
    parameters: list[ToolParameterDefinition] = []
    seen: set[str] = set()
    unmapped: list[str] = []

    def _replace(candidate: exp.Expr) -> exp.Expr:
        if not isinstance(candidate, _exp.Parameter):
            return candidate
        raw_name = candidate.this.name if hasattr(candidate.this, "name") else str(candidate.this)
        key = raw_name.lstrip("@:").lower()
        routine_parameter = by_name.get(key)
        if routine_parameter is None:
            unmapped.append(raw_name)
            return candidate
        family = physical_type_family(routine_parameter.physical_type)
        parameter_type = _PARAMETER_TYPE_BY_PHYSICAL_FAMILY.get(family)
        if parameter_type is None:
            unmapped.append(raw_name)
            return candidate
        if key not in seen:
            seen.add(key)
            parameters.append(
                ToolParameterDefinition(name=key, parameter_type=parameter_type, required=False)
            )
        return _exp.Placeholder(this=key)

    remapped = node.transform(_replace, copy=True)

    if unmapped:
        raise ProcedureNotEligibleError(
            "the procedure's result statement references a variable this "
            "generator cannot safely bind to a tool parameter: "
            f"{sorted(set(unmapped))!r} -- it must match a declared IN/INOUT "
            "routine parameter with a filterable type",
            code="PROCEDURE_UNBOUND_VARIABLE",
        )
    return remapped, parameters


def build_procedure_tool_blueprint(
    result_node: exp.Expr,
    routine_parameters: Sequence[RoutineInParameter],
    *,
    dialect: str,
    statement_count: int,
    sql_hash: str,
) -> ProcedureToolBlueprint:
    """Pure, DB-free given an already-resolved result-statement AST node.
    Deterministic in its inputs: the same node + routine_parameters always
    renders byte-identical SQL.

    Raises:
        ProcedureNotEligibleError: a literal survives in the statement, or a
            variable reference cannot be safely bound to a declared
            IN/INOUT routine parameter.
    """
    _require_no_literals(result_node)
    remapped, parameters = _remap_parameters(result_node, routine_parameters)

    referenced_tables = sorted(
        {
            table.sql(dialect=dialect)
            for table in remapped.find_all(_exp.Table)
        }
    )
    sql_template = remapped.sql(dialect=dialect, pretty=True)

    return ProcedureToolBlueprint(
        sql_template=sql_template,
        parameters=tuple(parameters),
        referenced_tables=tuple(referenced_tables),
        statement_count=statement_count,
        sql_hash=sql_hash,
    )


# ---------------------------------------------------------------------------
# The DB-touching half: resolve a routine, prove it read-only (or refuse
# naming exactly why), and select its one terminal result statement.
# ---------------------------------------------------------------------------


def find_single_read_only_result_statement(
    body_sql: str, dialect: str
) -> tuple[exp.Expr, ProcedureParseResult]:
    """N12 eligibility: the routine must parse fully (no UNPARSED chunk --
    "no write found" because parsing gave up is never mistaken for
    read-only), touch no INSERT/UPDATE/DELETE/MERGE/CREATE, and produce
    exactly one standalone result SELECT/UNION (no `INTO` target) -- more
    than one is ambiguous (which one is "the" output?) and zero means there
    is nothing to expose as a tool at all.

    Raises:
        ProcedureNotEligibleError: any of the above.
    """
    if not _SQLGLOT_AVAILABLE:
        raise ProcedureNotEligibleError(
            "sqlglot library is not available", code="PARSER_UNAVAILABLE"
        )

    statements: list[ParsedStatement] = walk_procedure_statements(body_sql, dialect)
    if not statements:
        raise ProcedureNotEligibleError(
            "no statements found in procedure body", code="PROCEDURE_EMPTY"
        )

    unparsed = [s for s in statements if s.is_unparsed]
    if unparsed:
        reasons = sorted({s.unparsed_reason for s in unparsed if s.unparsed_reason})
        raise ProcedureNotEligibleError(
            f"{len(unparsed)} statement(s) could not be parsed, so read-only "
            f"cannot be proven (not just \"no write statement found\"): {reasons}",
            code="PROCEDURE_NOT_FULLY_PARSED",
        )

    writes = [s for s in statements if s.is_write]
    if writes:
        raise ProcedureNotEligibleError(
            f"{len(writes)} write statement(s) found (INSERT/UPDATE/DELETE/"
            "MERGE/CREATE/SELECT INTO) -- not read-only",
            code="PROCEDURE_WRITES",
        )

    finals = [
        s for s in statements
        if not s.is_no_lineage and s.target_table == PROCEDURE_RESULT_TARGET and s.node is not None
    ]
    if not finals:
        raise ProcedureNotEligibleError(
            "no standalone result-producing SELECT statement found (nothing to expose as a tool)",
            code="PROCEDURE_NO_RESULT",
        )
    if len(finals) > 1:
        raise ProcedureNotEligibleError(
            f"{len(finals)} standalone result-producing SELECT statements found -- "
            "ambiguous which one is the procedure's output",
            code="PROCEDURE_AMBIGUOUS_RESULT",
        )

    result = parse_procedure_lineage(body_sql, dialect)
    node = finals[0].node
    assert node is not None  # narrowed by the `is not None` filter above
    return node, result


async def resolve_procedure_tool_source(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    routine_id: UUID,
    dialect: str,
) -> tuple[MetadataRoutine, exp.Expr, ProcedureParseResult, list[RoutineInParameter]]:
    """Fetch `routine_id`'s own captured body, gate it exactly like
    `routine_lineage_edges.require_eligible_routine_body`, prove it
    read-only with exactly one result statement, and load its declared
    IN/INOUT parameters -- everything `build_procedure_tool_blueprint`
    needs, resolved from real catalog/envelope state.

    Raises:
        ProcedureToolBlueprintError: unknown routine for this datasource.
        RoutineNotEligibleError: the routine's body is missing, withheld,
            unparsed, or quarantined (from `require_eligible_routine_body`).
        ProcedureNotEligibleError: not provably read-only, or not exactly
            one result statement.
    """
    routine = await session.get(MetadataRoutine, routine_id)
    if (
        routine is None
        or routine.datasource_id != datasource_id
        or routine.organization_id != organization_id
    ):
        raise ProcedureToolBlueprintError("unknown routine id for this datasource")
    # R11-FP03: a package is a namespace of subprograms, not one callable thing -- its body is
    # a spec and a body joined, and no tool can stand for it. Refused before the body is read.
    if routine.routine_type.strip().upper() == "PACKAGE":
        raise ProcedureNotEligibleError(
            "a package is not a callable routine; only its member subprograms are",
            code="PACKAGE_NOT_CALLABLE",
        )

    body = require_eligible_routine_body(routine)
    node, result = find_single_read_only_result_statement(body, dialect)

    parameter_rows = (
        await session.scalars(
            select(MetadataRoutineParameter)
            .where(
                MetadataRoutineParameter.routine_id == routine.id,
                MetadataRoutineParameter.status == "ACTIVE",
                MetadataRoutineParameter.mode.in_(("IN", "INOUT")),
            )
            .order_by(MetadataRoutineParameter.ordinal_position)
        )
    ).all()
    routine_parameters = [
        RoutineInParameter(name=row.name, physical_type=row.physical_type)
        for row in parameter_rows
        if row.name
    ]

    return routine, node, result, routine_parameters


# ---------------------------------------------------------------------------
# R11-FP14: is a hand-written tool this routine's extracted query?
# ---------------------------------------------------------------------------
#
# A tool this module generates records `source_routine_id` and the body's fingerprint, so
# R11-FP16 holds it when the routine moves. A person who writes the same query by hand gets no
# such link -- nothing in their SQL names the routine -- so their tool keeps answering after the
# routine's logic changes. Detecting that needs "is this the same query?" asked of two texts
# that can never be compared as strings: the routine's result statement comes from a body whose
# literals were redacted before storage (`sql_redaction`), and the tool's template went through
# `SqlGuard`, which re-rendered it and appended a row cap.
#
# So the comparison is made in the space the stored body already lives in. Both sides are parsed
# and rendered by sqlglot exactly as `SqlGuard` renders a tool's `normalized_sql` (the text a tool
# fingerprint is computed over); every value position becomes the redaction placeholder
# `sql_redaction` writes into a stored body; the guard's own row cap is removed; unquoted
# identifiers are normalised per dialect. What remains is structure: which tables, joined how,
# filtered on which columns, grouped and projected how. **No literal is ever compared** -- the
# routine side has none left to compare, and a tool that re-supplies a redacted value must still
# match its routine.

#: The placeholder `sql_redaction` substitutes for every literal in stored SQL. A literal, a bound
#: parameter and a placeholder all occupy the same *value slot*, and on the routine side most of
#: them already read as this.
_VALUE_SLOT: Final = "redacted"


def _is_value_slot(node: exp.Expr, parameter_names: frozenset[str]) -> bool:
    if isinstance(node, _exp.Literal | _exp.Placeholder | _exp.Parameter):
        return True
    # A PL/pgSQL or SQL-function body names its parameters bare (`WHERE d >= start_date`), where a
    # tool writes a placeholder. Only an *unqualified* name the routine itself declares as IN/INOUT
    # is treated as one: a qualified `t.start_date` is a column whatever the routine declares.
    return (
        bool(parameter_names)
        and isinstance(node, _exp.Column)
        and not node.table
        and node.name.lower() in parameter_names
    )


def _carries_logic(query: exp.Expr) -> bool:
    """Whether a query does anything beyond projecting plain columns of one table.

    **A bare projection is never matched.** `SELECT c.id, c.name FROM sales.customers AS c` in a
    routine and in a tool are the same text because it is the obvious query over that table, not
    because one was copied from the other -- and binding such a tool would hold it whenever the
    routine changes, for logic the tool never took from it. The binding exists to catch a
    routine's *logic* moving, so a query with none has nothing to bind.
    """
    if len(list(query.find_all(_exp.Table))) > 1:
        return True
    logic = (
        _exp.Where,
        _exp.Join,
        _exp.Group,
        _exp.Having,
        _exp.AggFunc,
        _exp.Window,
        _exp.With,
        _exp.Subquery,
        _exp.SetOperation,
        _exp.Distinct,
        _exp.Case,
    )
    if any(query.find(kind) is not None for kind in logic):
        return True
    if isinstance(query, _exp.Select):
        return any(
            not isinstance(projection.unalias(), _exp.Column) for projection in query.selects
        )
    return False


def structural_query_key(
    query: exp.Expr | str, *, dialect: str, parameter_names: Iterable[str] = ()
) -> str | None:
    """A digest of `query`'s structure with every value position erased, or None.

    None means "never match this": the text does not parse, is not a query, or is a bare
    projection (`_carries_logic`). `parameter_names` are the routine's declared IN/INOUT names,
    for a body that references them bare; a tool's template passes none, because its values are
    already placeholders.

    What deliberately does *not* match, so a false binding is not proposed:

    * a different table alias, a schema-qualified name on one side only, or reordered
      projections or predicates -- these are not normalised, because each is also how two
      genuinely different queries differ, and a miss costs a proposal where a false match costs
      a held tool;
    * a query that embeds the routine's query as a subquery, a CTE or one side of a join -- that
      is a different tool that *uses* the routine's logic, not the routine's query;
    * two queries that differ only in a value: they match, by design, since the routine's values
      are gone from storage. The person who confirms the binding sees both.

    The row cap `SqlGuard` appends to every tool (`LIMIT`/`TOP`/`FETCH`) is removed from the top
    level on both sides; a routine's own cap is removed with it, because the stored body's number
    was redacted and cannot be compared anyway.
    """
    if not _SQLGLOT_AVAILABLE:  # pragma: no cover
        return None
    if isinstance(query, str):
        try:
            node = _parse_one(query, read=dialect)
        except (_ParseError, _TokenError, ValueError):
            return None
    else:
        node = query.copy()
    if not isinstance(node, _exp.Query):
        return None
    node.set("limit", None)
    names = frozenset(name.lower() for name in parameter_names if name)
    node = node.transform(
        lambda part: _exp.Placeholder(this=_VALUE_SLOT) if _is_value_slot(part, names) else part,
        copy=False,
    )
    try:
        node = _normalize_identifiers(node, dialect=dialect)
        if not _carries_logic(node):
            return None
        rendered = node.sql(dialect=dialect, comments=False)
    except ValueError:
        # A dialect sqlglot does not know, or a node it cannot render in it: no key, so no
        # match -- the conservative answer, and never a failed agent item.
        return None
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def routine_result_query_key(
    routine: MetadataRoutine, parameter_names: Iterable[str], *, dialect: str
) -> str | None:
    """The structural key of the one result query this module would extract, or None.

    Exactly the routines tool generation would read: not a PACKAGE, a body that passes
    `require_eligible_routine_body`, parses fully, writes nothing and has one standalone result
    statement (`find_single_read_only_result_statement`). A routine that writes is not matched
    even when its last SELECT equals a tool's: its output depends on the writes before it, and
    "the routine's extracted query" is a thing this platform only defines for read-only routines.
    A literal in the result statement does *not* stop the match -- it is exactly why a person
    would have written the tool by hand, re-supplying the value generation refused to guess.
    """
    if routine.routine_type.strip().upper() == "PACKAGE":
        return None
    try:
        body = require_eligible_routine_body(routine)
        node, _result = find_single_read_only_result_statement(body, dialect)
    except (RoutineNotEligibleError, ProcedureNotEligibleError):
        return None
    return structural_query_key(node, dialect=dialect, parameter_names=parameter_names)
