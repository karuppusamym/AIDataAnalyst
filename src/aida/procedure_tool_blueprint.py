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

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import select

from aida.envelope_models import MetadataRoutine, MetadataRoutineParameter
from aida.procedure_lineage import (
    ParsedStatement,
    ProcedureParseResult,
    parse_procedure_lineage,
    walk_procedure_statements,
)
from aida.procedure_lineage_api import require_eligible_routine_body
from aida.relationship_naming import physical_type_family
from aida.schemas import ToolParameterDefinition
from aida.sql_lineage_parser import PROCEDURE_RESULT_TARGET
from aida.view_tool_blueprint import _PARAMETER_TYPE_BY_PHYSICAL_FAMILY

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlglot import exp

try:
    from sqlglot import exp as _exp

    _SQLGLOT_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SQLGLOT_AVAILABLE = False


class ProcedureToolBlueprintError(ValueError):
    """Raised for a structurally invalid blueprint request."""


class ProcedureNotEligibleError(ProcedureToolBlueprintError):
    """The routine failed N12's eligibility gate: not read-only-provable,
    or its single result statement cannot be safely reconstructed into an
    executable tool. Always names the specific reason -- never guessed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
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
            "procedure to take the value as a parameter"
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
            "routine parameter with a filterable type"
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
        raise ProcedureNotEligibleError("sqlglot library is not available")

    statements: list[ParsedStatement] = walk_procedure_statements(body_sql, dialect)
    if not statements:
        raise ProcedureNotEligibleError("no statements found in procedure body")

    unparsed = [s for s in statements if s.is_unparsed]
    if unparsed:
        reasons = sorted({s.unparsed_reason for s in unparsed if s.unparsed_reason})
        raise ProcedureNotEligibleError(
            f"{len(unparsed)} statement(s) could not be parsed, so read-only "
            f"cannot be proven (not just \"no write statement found\"): {reasons}"
        )

    writes = [s for s in statements if s.is_write]
    if writes:
        raise ProcedureNotEligibleError(
            f"{len(writes)} write statement(s) found (INSERT/UPDATE/DELETE/"
            "MERGE/CREATE/SELECT INTO) -- not read-only"
        )

    finals = [
        s for s in statements
        if not s.is_no_lineage and s.target_table == PROCEDURE_RESULT_TARGET and s.node is not None
    ]
    if not finals:
        raise ProcedureNotEligibleError(
            "no standalone result-producing SELECT statement found (nothing to expose as a tool)"
        )
    if len(finals) > 1:
        raise ProcedureNotEligibleError(
            f"{len(finals)} standalone result-producing SELECT statements found -- "
            "ambiguous which one is the procedure's output"
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
    `procedure_lineage_api.require_eligible_routine_body`, prove it
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
