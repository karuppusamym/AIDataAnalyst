import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlglot import exp, parse_one

from aida.schemas import ToolParameterDefinition


class ToolParameterError(ValueError):
    pass


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
            raise ToolParameterError(f"required parameter is null: {definition.name}")
        return None
    parameter_type = definition.parameter_type
    if parameter_type == "STRING":
        if not isinstance(value, str):
            raise ToolParameterError(f"parameter must be a string: {definition.name}")
        normalized: Any = value
        if definition.max_length is not None and len(value) > definition.max_length:
            raise ToolParameterError(f"parameter exceeds max_length: {definition.name}")
    elif parameter_type == "INTEGER":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolParameterError(f"parameter must be an integer: {definition.name}")
        normalized = value
    elif parameter_type == "NUMBER":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ToolParameterError(f"parameter must be numeric: {definition.name}")
        if not math.isfinite(float(value)):
            raise ToolParameterError(f"parameter must be finite: {definition.name}")
        normalized = value
    elif parameter_type == "BOOLEAN":
        if not isinstance(value, bool):
            raise ToolParameterError(f"parameter must be boolean: {definition.name}")
        normalized = value
    elif parameter_type == "DATE":
        if not isinstance(value, str):
            raise ToolParameterError(f"parameter must be an ISO date string: {definition.name}")
        try:
            normalized = date.fromisoformat(value).isoformat()
        except ValueError as exc:
            raise ToolParameterError(
                f"parameter must be an ISO date string: {definition.name}"
            ) from exc
    else:
        raise ToolParameterError(f"unsupported parameter type: {parameter_type}")

    if definition.allowed_values is not None and normalized not in definition.allowed_values:
        raise ToolParameterError(f"parameter value is not allowed: {definition.name}")
    if isinstance(normalized, int | float) and not isinstance(normalized, bool):
        if definition.minimum is not None and normalized < definition.minimum:
            raise ToolParameterError(f"parameter is below minimum: {definition.name}")
        if definition.maximum is not None and normalized > definition.maximum:
            raise ToolParameterError(f"parameter exceeds maximum: {definition.name}")
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
        missing_definitions = sorted(placeholders - set(declared))
        unused_definitions = sorted(set(declared) - placeholders)
        details = []
        if missing_definitions:
            details.append(f"undeclared placeholders: {', '.join(missing_definitions)}")
        if unused_definitions:
            details.append(f"unused parameter definitions: {', '.join(unused_definitions)}")
        raise ToolParameterError("; ".join(details))
    unknown_values = sorted(set(values) - set(declared))
    if unknown_values:
        raise ToolParameterError(f"unknown parameters: {', '.join(unknown_values)}")

    normalized: dict[str, Any] = {}
    for name, definition in declared.items():
        if name in values:
            value = values[name]
        elif definition.default is not None:
            value = definition.default
        elif definition.required:
            raise ToolParameterError(f"required parameter is missing: {name}")
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
