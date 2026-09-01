"""Typed authoring surface for a governed-tool candidate.

``ToolCandidate`` is the only object a third-party developer builds by hand.
Its parameter list is made of ``aida.schemas.ToolParameterDefinition``
instances directly (via the :func:`parameter` helper below) rather than a
parallel SDK-only parameter type, so the exact same pydantic validation the
server applies to a parameter definition (name pattern, min/max bounds,
"sensitive parameters cannot define persisted defaults", ...) already runs
the moment an author constructs one -- there is nothing to keep in sync.

Nothing on this class or module can publish, approve, certify, or execute a
tool -- see the package docstring in ``aida_tool_sdk/__init__.py``.
"""

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from aida.schemas import ToolParameterDefinition

#: Re-exported so callers only need `from aida_tool_sdk import parameter` --
#: it is the exact server-side parameter model, not a copy of it.
parameter = ToolParameterDefinition


@dataclass(slots=True)
class ToolCandidate:
    """A candidate governed-tool version, authored offline.

    Attributes mirror ``aida.schemas.GovernedToolVersionCreate`` (the
    draft-submission request body) plus two locally-only fields --
    ``dialect`` and ``example_values`` -- that exist purely to drive local
    validation/rendering and are never sent to the server: the server
    derives the SQL dialect from the ``datasource_id`` you provide, and
    executing a published tool is a maker-checker-gated, server-side action
    this SDK deliberately has no access to.

    Args:
        slug: Stable tool identifier within a project. Must match
            ``^[a-z][a-z0-9_]{1,99}$`` (enforced by the server's
            ``GovernedToolVersionCreate.slug``, and by construction below).
        name: Human-readable tool name (2-200 chars).
        description: What the tool does (3-4000 chars).
        datasource_id: The target ``DataSource`` this tool will query against.
            The SDK cannot validate this exists or belongs to your project --
            only the server can, at submission time.
        sql_template: A single read-only SQL statement using named
            placeholders (``:param_name``) for every parameter.
        dialect: The SQL dialect to parse/validate the template with locally
            (e.g. ``"postgres"``, ``"snowflake"``, ``"bigquery"``, ``"tsql"``,
            ``"oracle"``) -- must match the target datasource's actual
            dialect for local validation to mean anything, but it is *not*
            part of the wire payload; the server resolves the real dialect
            from ``datasource_id`` itself.
        parameters: Named placeholders' definitions, built with
            :func:`parameter` (an alias for
            ``aida.schemas.ToolParameterDefinition``).
        allowed_roles: Roles permitted to invoke the *published* tool. Must
            be non-empty and unique (enforced server-side too).
        semantic_model_version_id: Optional published semantic model this
            tool is scoped to.
        example_values: Optional example parameter values used only for the
            local rendering dry-run (:func:`aida_tool_sdk.validation.validate_candidate`).
            Never submitted to the server.
    """

    slug: str
    name: str
    description: str
    datasource_id: UUID
    sql_template: str
    dialect: str
    parameters: list[ToolParameterDefinition] = field(default_factory=list)
    allowed_roles: list[str] = field(default_factory=list)
    semantic_model_version_id: UUID | None = None
    example_values: dict[str, Any] = field(default_factory=dict)

    def add_parameter(self, param: ToolParameterDefinition) -> "ToolCandidate":
        """Append a parameter definition and return ``self`` for chaining."""
        self.parameters.append(param)
        return self

    def with_example_values(self, **values: Any) -> "ToolCandidate":
        """Merge example parameter values (for the local rendering dry-run
        only -- never submitted) and return ``self`` for chaining."""
        self.example_values.update(values)
        return self
