"""R11-FP14: stage a governed tool DRAFT -- the one write path for a new `GovernedToolVersion`.

Extracted from `tool_api._persist_tool_version_draft` so the tool agent can create a draft inside
its own per-item savepoint: this function validates, adds and flushes, records the audit and outbox
rows, and returns. It never commits and never raises an HTTP error. The router keeps both around
this call, so every existing caller answers exactly as before -- a refusal is still a 422 carrying
the same sentence, a unique-key conflict still a 409.

Validation is the same whoever drafts: the template's placeholders must match the declared
parameters exactly, `SqlGuard` must accept it, and every table it references must be one the
gateway allows for the datasource. Drafting publishes nothing -- a draft reaches PUBLISHED only
through a `GOVERNED_TOOL_VERSION` review a person decides.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.config import Settings
from aida.context import get_correlation_id
from aida.events import record_audit, record_outbox
from aida.models import DataSource, GovernedTool, GovernedToolVersion, Project
from aida.query_gateway import QueryExecutionGateway
from aida.schemas import GovernedToolVersionCreate
from aida.security import SecurityContext
from aida.sql_guard import SqlGuard
from aida.tool_rendering import template_placeholders

REFUSED_TEMPLATE_UNPARSEABLE: Final = "TEMPLATE_UNPARSEABLE"
REFUSED_PLACEHOLDER_MISMATCH: Final = "PLACEHOLDER_MISMATCH"
REFUSED_SQL_GUARD: Final = "SQL_GUARD_REFUSED"
REFUSED_TABLES_NOT_ALLOWED: Final = "TABLES_NOT_ALLOWED"


class ToolDraftRefused(ValueError):
    """The template cannot become a draft. `code` is stable and value-free; `detail` is the
    sentence the HTTP route has always answered with."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(detail)


async def stage_tool_version_draft(
    session: AsyncSession,
    project: Project,
    datasource: DataSource,
    body: GovernedToolVersionCreate,
    *,
    audit_context: SecurityContext,
    settings: Settings,
) -> tuple[GovernedTool, GovernedToolVersion]:
    """Validate `body` and stage a new DRAFT version of the project's tool with its slug."""
    definitions = body.parameters
    declared = {definition.name for definition in definitions}
    try:
        placeholders = template_placeholders(body.sql_template, dialect=datasource.dialect)
    except Exception as exc:
        raise ToolDraftRefused(
            REFUSED_TEMPLATE_UNPARSEABLE, "tool SQL template cannot be parsed"
        ) from exc
    if placeholders != declared:
        raise ToolDraftRefused(
            REFUSED_PLACEHOLDER_MISMATCH,
            "SQL placeholders must exactly match parameter definitions",
        )
    guard = SqlGuard(
        default_row_limit=settings.default_query_row_limit,
        hard_row_limit=settings.hard_query_row_limit,
    )
    validation = guard.validate(body.sql_template, dialect=datasource.dialect)
    if not validation.valid or not validation.normalized_sql:
        raise ToolDraftRefused(
            REFUSED_SQL_GUARD,
            f"invalid governed tool SQL: {', '.join(validation.violations)}",
        )
    gateway = QueryExecutionGateway(settings)
    allowed_tables = await gateway.allowed_tables(session, datasource)
    unauthorized = sorted(
        table for table in validation.referenced_tables if table.lower() not in allowed_tables
    )
    if unauthorized:
        raise ToolDraftRefused(
            REFUSED_TABLES_NOT_ALLOWED,
            f"unknown or unauthorized tool tables: {', '.join(unauthorized)}",
        )

    tool = await session.scalar(
        select(GovernedTool).where(
            GovernedTool.project_id == project.id,
            GovernedTool.slug == body.slug,
        )
    )
    if tool is None:
        tool = GovernedTool(
            organization_id=project.organization_id,
            project_id=project.id,
            slug=body.slug,
        )
        session.add(tool)
        await session.flush()
    latest = await session.scalar(
        select(func.max(GovernedToolVersion.version)).where(GovernedToolVersion.tool_id == tool.id)
    )
    fingerprint_payload = {
        "name": body.name,
        "description": body.description,
        "datasource_id": str(body.datasource_id),
        "semantic_model_version_id": (
            str(body.semantic_model_version_id) if body.semantic_model_version_id else None
        ),
        "sql_template": validation.normalized_sql,
        "referenced_tables": sorted(validation.referenced_tables),
        "parameters": [definition.model_dump(mode="json") for definition in definitions],
        "allowed_roles": sorted(body.allowed_roles),
    }
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    version = GovernedToolVersion(
        organization_id=project.organization_id,
        tool_id=tool.id,
        version=(latest or 0) + 1,
        name=body.name,
        description=body.description,
        datasource_id=datasource.id,
        semantic_model_version_id=body.semantic_model_version_id,
        sql_template=validation.normalized_sql,
        referenced_tables=sorted(validation.referenced_tables),
        parameter_schema=[definition.model_dump(mode="json") for definition in definitions],
        allowed_roles=sorted(body.allowed_roles),
        fingerprint=fingerprint,
        created_by=audit_context.principal_id,
    )
    session.add(version)
    await session.flush()
    record_audit(
        session,
        replace(audit_context, organization_id=project.organization_id),
        action="tool.version.create",
        resource_type="governed_tool_version",
        resource_id=str(version.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={"tool_slug": tool.slug, "version": version.version},
    )
    record_outbox(
        session,
        organization_id=project.organization_id,
        aggregate_type="governed_tool_version",
        aggregate_id=str(version.id),
        event_type="tool.version.draft_created.v1",
        payload={
            "tool_version_id": str(version.id),
            "tool_id": str(tool.id),
            "project_id": str(project.id),
        },
    )
    return tool, version
