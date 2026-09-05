"""Resolve plan steps against governed tool versions; never execute arbitrary SQL."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import DataSource, GovernedToolVersion
from aida.schemas import ToolParameterDefinition
from aida.security import SecurityContext
from aida.tool_plans import ToolPlan, ValidationIssue, ValidationResult, validate_plan
from aida.tool_rendering import ToolParameterError, render_tool_sql


async def resolve_plan_tools(
    session: AsyncSession,
    plan: ToolPlan,
    context: SecurityContext,
) -> tuple[ValidationResult, dict[int, UUID]]:
    issues = list(validate_plan(plan).issues)
    versions: dict[int, UUID] = {}
    for step in plan.steps:
        try:
            tool_id = UUID(step.tool_id)
            version_number = int(step.tool_version)
        except ValueError:
            issues.append(
                ValidationIssue(step.sequence, "Select a published tool and version", "ERROR")
            )
            continue
        version = await session.scalar(
            select(GovernedToolVersion).where(
                GovernedToolVersion.tool_id == tool_id,
                GovernedToolVersion.version == version_number,
                GovernedToolVersion.organization_id == context.require_organization(),
                GovernedToolVersion.status == "PUBLISHED",
            )
        )
        if version is None:
            issues.append(
                ValidationIssue(
                    step.sequence, "Tool version is unavailable or unpublished", "ERROR"
                )
            )
            continue
        if "PlatformAdmin" not in context.roles and (
            context.roles.isdisjoint(version.allowed_roles)
            or context.roles.isdisjoint({"Analyst", "AgentDeveloper", "ToolConsumer"})
        ):
            issues.append(
                ValidationIssue(step.sequence, "Tool execution permission denied", "ERROR")
            )
            continue
        datasource = await session.get(DataSource, version.datasource_id)
        if datasource is None:
            issues.append(ValidationIssue(step.sequence, "Tool datasource is unavailable", "ERROR"))
            continue
        try:
            render_tool_sql(
                version.sql_template,
                dialect=datasource.dialect,
                definitions=[
                    ToolParameterDefinition.model_validate(p) for p in version.parameter_schema
                ],
                values=step.parameters,
            )
        except (ToolParameterError, HTTPException):
            issues.append(
                ValidationIssue(
                    step.sequence, "Parameters do not satisfy the tool contract", "ERROR"
                )
            )
            continue
        versions[step.sequence] = version.id
    return ValidationResult(not any(i.severity == "ERROR" for i in issues), issues), versions
