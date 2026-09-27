import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any, Final, Self

from sqlglot import exp, parse_one

from aida.schemas import ToolParameterDefinition


class ToolParameterCode(StrEnum):
    """Why a parameter did not bind. Stable and value-free: a code names the condition, never the
    value that met it, and it does not change when the message's wording does.

    The renderer is shared -- governed tools, the Studio contract check, tool certification and the
    reviewed-SQL workspace all read its refusals -- and until these existed each consumer that
    wanted to tell one refusal from another matched the message text.
    """

    #: A refusal raised with a message and no issue of its own (`ToolParameterError("...")`).
    UNCLASSIFIED = "UNCLASSIFIED"
    #: The template names a `:placeholder` no definition declares.
    UNDECLARED_PLACEHOLDER = "UNDECLARED_PLACEHOLDER"
    #: A definition names a parameter the template has no placeholder for.
    UNUSED_DEFINITION = "UNUSED_DEFINITION"
    #: A value was supplied for a parameter nothing declares.
    UNKNOWN_PARAMETER = "UNKNOWN_PARAMETER"
    #: A required parameter has neither a value nor a default.
    REQUIRED_MISSING = "REQUIRED_MISSING"
    #: A required parameter's value is null.
    REQUIRED_NULL = "REQUIRED_NULL"
    NOT_A_STRING = "NOT_A_STRING"
    NOT_AN_INTEGER = "NOT_AN_INTEGER"
    NOT_NUMERIC = "NOT_NUMERIC"
    NOT_FINITE = "NOT_FINITE"
    NOT_A_BOOLEAN = "NOT_A_BOOLEAN"
    NOT_AN_ISO_DATE = "NOT_AN_ISO_DATE"
    TOO_LONG = "TOO_LONG"
    #: The definition's `parameter_type` is not one the renderer binds.
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    NOT_ALLOWED = "NOT_ALLOWED"
    BELOW_MINIMUM = "BELOW_MINIMUM"
    ABOVE_MAXIMUM = "ABOVE_MAXIMUM"


#: The wording each code has always had. It is what `str(ToolParameterError)` says -- Studio's
#: contract check, the tool API and tool certification show it, and tests match it -- so it is
#: defined here, once, and the message is built from it rather than typed at each raise.
_PHRASES: Final[dict[ToolParameterCode, str]] = {
    ToolParameterCode.UNCLASSIFIED: "parameters could not be bound",
    ToolParameterCode.UNDECLARED_PLACEHOLDER: "undeclared placeholders",
    ToolParameterCode.UNUSED_DEFINITION: "unused parameter definitions",
    ToolParameterCode.UNKNOWN_PARAMETER: "unknown parameters",
    ToolParameterCode.REQUIRED_MISSING: "required parameter is missing",
    ToolParameterCode.REQUIRED_NULL: "required parameter is null",
    ToolParameterCode.NOT_A_STRING: "parameter must be a string",
    ToolParameterCode.NOT_AN_INTEGER: "parameter must be an integer",
    ToolParameterCode.NOT_NUMERIC: "parameter must be numeric",
    ToolParameterCode.NOT_FINITE: "parameter must be finite",
    ToolParameterCode.NOT_A_BOOLEAN: "parameter must be boolean",
    ToolParameterCode.NOT_AN_ISO_DATE: "parameter must be an ISO date string",
    ToolParameterCode.TOO_LONG: "parameter exceeds max_length",
    ToolParameterCode.UNSUPPORTED_TYPE: "unsupported parameter type",
    ToolParameterCode.NOT_ALLOWED: "parameter value is not allowed",
    ToolParameterCode.BELOW_MINIMUM: "parameter is below minimum",
    ToolParameterCode.ABOVE_MAXIMUM: "parameter exceeds maximum",
}


@dataclass(frozen=True, slots=True)
class ToolParameterIssue:
    """One thing wrong: its code, and the parameter names it concerns.

    `names` are parameter names -- declared or found in the template -- never values. The one
    exception is UNSUPPORTED_TYPE, whose subject is the declared `parameter_type` text.
    """

    code: ToolParameterCode
    names: tuple[str, ...] = ()


class ToolParameterError(ValueError):
    """A template and its parameters that do not bind.

    `str()` is the message every existing consumer reads. `issues` is the same refusal as data: one
    per thing wrong, each with a `ToolParameterCode`, so a caller decides by code instead of by
    matching words. One refusal can carry several (a template can name an undeclared placeholder
    *and* a definition can go unused). A message raised without issues carries one UNCLASSIFIED.
    """

    def __init__(self, message: str, *, issues: Sequence[ToolParameterIssue] = ()) -> None:
        super().__init__(message)
        self.issues: tuple[ToolParameterIssue, ...] = tuple(issues) or (
            ToolParameterIssue(ToolParameterCode.UNCLASSIFIED),
        )

    @classmethod
    def refusing(cls, *issues: ToolParameterIssue) -> Self:
        """The refusal for these issues, worded as it always has been: `phrase: name, name`."""
        message = "; ".join(
            f"{_PHRASES[issue.code]}: {', '.join(issue.names)}" if issue.names
            else _PHRASES[issue.code]
            for issue in issues
        )
        return cls(message, issues=issues)

    @property
    def code(self) -> ToolParameterCode:
        """The code of the first issue -- the whole refusal's, when it carries only one."""
        return self.issues[0].code


def _refusal(code: ToolParameterCode, *names: str) -> ToolParameterError:
    return ToolParameterError.refusing(ToolParameterIssue(code, names))


@dataclass(frozen=True, slots=True)
class RenderedToolSql:
    sql: str
    normalized_parameters: dict[str, Any]


def template_placeholders(sql_template: str, *, dialect: str) -> set[str]:
    statement = parse_one(sql_template, read=dialect)
    return {placeholder.name for placeholder in statement.find_all(exp.Placeholder)}


def _normalize_value(definition: ToolParameterDefinition, value: Any) -> Any:
    if value is None:
        if definition.required:
            raise _refusal(ToolParameterCode.REQUIRED_NULL, definition.name)
        return None
    parameter_type = definition.parameter_type
    if parameter_type == "STRING":
        if not isinstance(value, str):
            raise _refusal(ToolParameterCode.NOT_A_STRING, definition.name)
        normalized: Any = value
        if definition.max_length is not None and len(value) > definition.max_length:
            raise _refusal(ToolParameterCode.TOO_LONG, definition.name)
    elif parameter_type == "INTEGER":
        if isinstance(value, bool) or not isinstance(value, int):
            raise _refusal(ToolParameterCode.NOT_AN_INTEGER, definition.name)
        normalized = value
    elif parameter_type == "NUMBER":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise _refusal(ToolParameterCode.NOT_NUMERIC, definition.name)
        if not math.isfinite(float(value)):
            raise _refusal(ToolParameterCode.NOT_FINITE, definition.name)
        normalized = value
    elif parameter_type == "BOOLEAN":
        if not isinstance(value, bool):
            raise _refusal(ToolParameterCode.NOT_A_BOOLEAN, definition.name)
        normalized = value
    elif parameter_type == "DATE":
        if not isinstance(value, str):
            raise _refusal(ToolParameterCode.NOT_AN_ISO_DATE, definition.name)
        try:
            normalized = date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise _refusal(ToolParameterCode.NOT_AN_ISO_DATE, definition.name) from exc
    else:
        raise _refusal(ToolParameterCode.UNSUPPORTED_TYPE, parameter_type)

    if definition.allowed_values is not None and normalized not in definition.allowed_values:
        raise _refusal(ToolParameterCode.NOT_ALLOWED, definition.name)
    if isinstance(normalized, int | float) and not isinstance(normalized, bool):
        if definition.minimum is not None and normalized < definition.minimum:
            raise _refusal(ToolParameterCode.BELOW_MINIMUM, definition.name)
        if definition.maximum is not None and normalized > definition.maximum:
            raise _refusal(ToolParameterCode.ABOVE_MAXIMUM, definition.name)
    return normalized


def render_tool_sql(
    sql_template: str,
    *,
    dialect: str,
    definitions: list[ToolParameterDefinition],
    values: dict[str, Any],
) -> RenderedToolSql:
    declared = {definition.name: definition for definition in definitions}
    placeholders = template_placeholders(sql_template, dialect=dialect)
    if placeholders != set(declared):
        issues: list[ToolParameterIssue] = []
        missing_definitions = sorted(placeholders - set(declared))
        unused_definitions = sorted(set(declared) - placeholders)
        if missing_definitions:
            issues.append(
                ToolParameterIssue(
                    ToolParameterCode.UNDECLARED_PLACEHOLDER, tuple(missing_definitions)
                )
            )
        if unused_definitions:
            issues.append(
                ToolParameterIssue(ToolParameterCode.UNUSED_DEFINITION, tuple(unused_definitions))
            )
        raise ToolParameterError.refusing(*issues)
    unknown_values = sorted(set(values) - set(declared))
    if unknown_values:
        raise _refusal(ToolParameterCode.UNKNOWN_PARAMETER, *unknown_values)

    normalized: dict[str, Any] = {}
    for name, definition in declared.items():
        if name in values:
            value = values[name]
        elif definition.default is not None:
            value = definition.default
        elif definition.required:
            raise _refusal(ToolParameterCode.REQUIRED_MISSING, name)
        else:
            value = None
        normalized[name] = _normalize_value(definition, value)

    statement = parse_one(sql_template, read=dialect)
    rendered = statement.transform(
        lambda node: exp.convert(normalized[node.name])
        if isinstance(node, exp.Placeholder)
        else node
    )
    return RenderedToolSql(
        sql=rendered.sql(dialect=dialect, pretty=True),
        normalized_parameters=normalized,
    )
