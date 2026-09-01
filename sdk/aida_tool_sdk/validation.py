"""Local, fast, offline validation for a `ToolCandidate`.

Everything here reuses the server's real validation code -- it never
reimplements SQL safety or parameter-rendering rules that could quietly
drift from `aida.tool_api`/`aida.sql_guard`/`aida.tool_rendering`:

1. Wire-shape validation: `aida_tool_sdk.serialization.candidate_to_wire_model`
   constructs the real `aida.schemas.GovernedToolVersionCreate`.
2. Placeholder/parameter parity: `aida.tool_rendering.template_placeholders`
   parses the SQL template (via `sqlglot`) and returns its named
   placeholders; those are compared against the declared parameter names,
   the same equality check `aida.tool_api.create_tool_version` runs before
   accepting a draft (lines ~223-233 there). That comparison itself is a
   two-line set-equality check, not a rule with independent behavior to
   drift -- reimplementing the *comparison* is unavoidable outside the
   endpoint function, but the thing being compared (`template_placeholders`)
   is the server's own function, not a copy of it.
3. SQL safety: `aida.sql_guard.SqlGuard.validate` -- the exact allowlist /
   denylist guard (single read-only statement, no mutation, no
   `SELECT *`, no forbidden functions, bounded joins, row limit) the server
   runs before a draft is ever accepted.
4. Rendering dry-run: if the candidate carries `example_values`,
   `aida.tool_rendering.render_tool_sql` -- the exact function used both at
   draft-creation time and at tool-*execution* time -- is run against them,
   so an author can see the literal SQL their example arguments would
   produce before ever submitting anything.

What this cannot check locally: whether `datasource_id` exists, its real
dialect, whether a `semantic_model_version_id` is published, and whether
every referenced table is on that datasource's allowed list. Those require
live server/database state and are enforced by
`aida.tool_api.create_tool_version` itself when you submit.
"""

from dataclasses import dataclass, field
from typing import Any

from aida.sql_guard import SqlGuard
from aida.tool_rendering import ToolParameterError, render_tool_sql, template_placeholders
from aida_tool_sdk.candidate import ToolCandidate
from aida_tool_sdk.errors import ToolCandidateValidationError
from aida_tool_sdk.serialization import candidate_to_wire_model

# Mirrors atlas.platform.config.PlatformConfig's own field defaults
# (`default_query_row_limit=5000`, `hard_query_row_limit=100_000`) -- the
# values a freshly-configured server would apply. The *organization's*
# actual configured limits are only known to the server; these defaults only
# affect the `LIMIT` `SqlGuard` injects into `normalized_sql` below, never
# whether the template is otherwise valid.
DEFAULT_ROW_LIMIT = 5_000
HARD_ROW_LIMIT = 100_000


@dataclass(slots=True)
class LocalValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)
    normalized_sql: str | None = None
    referenced_tables: tuple[str, ...] = ()
    rendered_example_sql: str | None = None
    rendered_example_parameters: dict[str, Any] | None = None


def validate_candidate(
    candidate: ToolCandidate,
    *,
    default_row_limit: int = DEFAULT_ROW_LIMIT,
    hard_row_limit: int = HARD_ROW_LIMIT,
    raise_on_error: bool = False,
) -> LocalValidationResult:
    """Run every local check against `candidate` and return a combined result.

    Checks stop being meaningful past the first structural failure (an
    unparseable template, or a wire-shape violation) -- in that case
    `errors` holds just that failure rather than a cascade of derived ones.

    Args:
        raise_on_error: if True, raise `ToolCandidateValidationError` instead
            of returning an invalid result. Off by default so a caller can
            inspect every error at once (e.g. to show them all in a CLI).
    """
    errors: list[str] = []

    try:
        candidate_to_wire_model(candidate)
    except ToolCandidateValidationError as exc:
        errors.extend(exc.errors)
        result = LocalValidationResult(valid=False, errors=errors)
        if raise_on_error:
            raise ToolCandidateValidationError(errors) from exc
        return result

    declared = {p.name for p in candidate.parameters}
    try:
        placeholders = template_placeholders(candidate.sql_template, dialect=candidate.dialect)
    except Exception as exc:  # sqlglot raises its own, dialect-specific errors
        errors.append(f"SQL template cannot be parsed for dialect {candidate.dialect!r}: {exc}")
        result = LocalValidationResult(valid=False, errors=errors)
        if raise_on_error:
            raise ToolCandidateValidationError(errors) from exc
        return result

    if placeholders != declared:
        missing = sorted(placeholders - declared)
        unused = sorted(declared - placeholders)
        if missing:
            errors.append(f"SQL placeholders undeclared as parameters: {', '.join(missing)}")
        if unused:
            errors.append(f"parameter definitions unused in the SQL template: {', '.join(unused)}")

    guard = SqlGuard(default_row_limit=default_row_limit, hard_row_limit=hard_row_limit)
    guard_result = guard.validate(candidate.sql_template, dialect=candidate.dialect)
    if not guard_result.valid:
        errors.extend(f"SQL_GUARD:{violation}" for violation in guard_result.violations)

    rendered_sql: str | None = None
    rendered_parameters: dict[str, Any] | None = None
    if candidate.example_values and not errors:
        try:
            rendered = render_tool_sql(
                candidate.sql_template,
                dialect=candidate.dialect,
                definitions=list(candidate.parameters),
                values=candidate.example_values,
            )
        except ToolParameterError as exc:
            errors.append(f"example rendering failed: {exc}")
        else:
            rendered_sql = rendered.sql
            rendered_parameters = rendered.normalized_parameters

    result = LocalValidationResult(
        valid=not errors,
        errors=errors,
        normalized_sql=guard_result.normalized_sql,
        referenced_tables=guard_result.referenced_tables,
        rendered_example_sql=rendered_sql,
        rendered_example_parameters=rendered_parameters,
    )
    if raise_on_error and not result.valid:
        raise ToolCandidateValidationError(errors)
    return result
