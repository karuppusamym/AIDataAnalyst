"""The governed tool execution path itself: one function, every surface (R11-GQL01).

`execute_tool_version` is what a REST execute route, a persisted tool plan and GraphQL's
`executeGovernedTool` all run, with the agent-contract check it makes first. It lived beside
the router in `aida.tool_api` until GraphQL needed it: a GraphQL module may not import a
router, and `aida.governed_execution` reached one only to find this function. It is moved
unchanged, `aida.tool_api` re-exports both names for the importers it already had, and the
import contract now holds every module the GraphQL facade reaches to that line.

The execution's own rules are unchanged and are documented on the function: the role gate,
the published-version and role-binding checks, the agent contract, quality holds, the
rendered statement, the gateway's own scope and the recorded `ToolExecution` row.
"""

import json
from dataclasses import replace
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_contracts import (
    AgentContractValidationError,
    agent_kill_blocking_reason,
    envelope_violation,
    load_contract_for_principal,
)
from aida.config import Settings
from aida.context import get_correlation_id
from aida.context_product_execution_scope import ContextProductExecutionScope
from aida.events import record_audit, record_outbox
from aida.fleet import RunAdmissionRejected, ensure_datasource_enabled
from aida.models import (
    DataSource,
    GovernedTool,
    GovernedToolVersion,
    ToolExecution,
)
from aida.quality_coupling import check_tool_gate, fetch_open_incidents, resolve_table_ids
from aida.query_execution_view import query_execution_response
from aida.query_gateway import QueryExecutionGateway, QueryRejected
from aida.schemas import (
    ToolExecutionRequest,
    ToolExecutionResponse,
    ToolParameterDefinition,
)
from aida.security import SecurityContext, enforce_organization
from aida.signing import sign_value
from aida.tool_rendering import ToolParameterError, render_tool_sql
from aida.tool_source_binding import SOURCE_CHANGED_MESSAGE, fetch_source_binding_holds


async def _enforce_agent_contract(
    session: AsyncSession,
    context: SecurityContext,
    *,
    version: GovernedToolVersion,
    tool: GovernedTool,
) -> None:
    """R11-C6: hold a contracted agent to its contract on *this* path too.

    The orchestrator has gated agent execution on the kill switch and the
    capability envelope since AG-10, and this route -- the direct HTTP
    execution of a published governed tool version -- did not. An agent
    identity holding any of the four execution roles could therefore execute a
    governed tool with its kill switch engaged and outside the `tool_slugs` its
    contract names, simply by addressing the tool version by id instead of
    asking through Ask. Same authority, same governed object, different
    transport: the transport is not the control.

    The asymmetry was already reasoned about here in the other direction. The
    orchestrator carries a comment requiring parity with this route's quality
    gate, because a tool version "must not answer differently depending on
    which surface asked for it". That argument does not run one way only, and
    the contract half of it had no such note.

    Ordering is deliberate: this sits above `ensure_datasource_enabled` and the
    quality gate, so a blocked agent learns nothing about the datasource's
    state or which of the tool's dependencies have open incidents. A refusal
    that discloses is still a disclosure.

    A human caller has no contract and is unaffected -- `load_contract_for_principal`
    returns `None` -- while an `AGENT`-typed identity with no contract, or with
    an ambiguous one, is refused there rather than served as an uncontracted
    human.
    """
    try:
        contract = await load_contract_for_principal(
            session,
            organization_id=version.organization_id,
            agent_principal_id=context.principal_id,
            principal_type=context.principal_type,
        )
    except AgentContractValidationError as exc:
        raise HTTPException(status_code=403, detail=exc.code) from exc
    if contract is None:
        return
    reason = await agent_kill_blocking_reason(session, contract)
    if reason is None:
        reason = envelope_violation(contract, tool_slug=tool.slug)
    if reason is None:
        return
    record_audit(
        session,
        replace(context, organization_id=version.organization_id),
        action="tool.execute",
        resource_type="governed_tool_version",
        resource_id=str(version.id),
        outcome="DENIED",
        correlation_id=get_correlation_id(),
        details={
            "reason": reason,
            "agent_principal_id": contract.agent_principal_id,
            "kill_scope": contract.kill_scope,
            "tool_slug": tool.slug,
        },
    )
    await session.commit()
    raise HTTPException(status_code=403, detail=reason)


async def execute_tool_version(
    version_id: UUID,
    body: ToolExecutionRequest,
    context: SecurityContext,
    session: AsyncSession,
    settings: Settings,
    *,
    context_product_scope: ContextProductExecutionScope | None = None,
    tool_execution_id: UUID | None = None,
) -> ToolExecutionResponse:
    """Shared governed execution path for HTTP callers, persisted tool plans and GraphQL.

    `context_product_scope` (R11-GQL02) holds the rendered statement to a published product's
    tables at the gateway, exactly as Ask and the direct-SQL route are held; the REST route
    passes none, so its behaviour is unchanged.

    `tool_execution_id` (R11-GQL02) is the id the `ToolExecution` row will carry, chosen by a
    caller that recorded it first -- so an execution whose outcome that caller never heard can
    later be settled from this row rather than guessed at.
    """
    if context.roles.isdisjoint({"PlatformAdmin", "Analyst", "AgentDeveloper", "ToolConsumer"}):
        raise HTTPException(status_code=403, detail="tool execution role is required")
    version = await session.get(GovernedToolVersion, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="tool version not found")
    enforce_organization(context, version.organization_id)
    if version.status != "PUBLISHED":
        raise HTTPException(status_code=409, detail="only a published tool can execute")
    if "PlatformAdmin" not in context.roles and context.roles.isdisjoint(version.allowed_roles):
        raise HTTPException(status_code=403, detail="tool role binding denied execution")
    tool = await session.get(GovernedTool, version.tool_id)
    datasource = await session.get(DataSource, version.datasource_id)
    if tool is None or datasource is None:
        raise HTTPException(status_code=409, detail="tool dependency is unavailable")
    await _enforce_agent_contract(session, context, version=version, tool=tool)
    try:
        ensure_datasource_enabled(datasource)
    except RunAdmissionRejected as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # TL-3: gate execution on open quality incidents against the tool's own
    # declared dependencies (`version.referenced_tables`, authorised at
    # tool-version creation time) -- resolved to this datasource's tables and
    # checked before a single row of SQL is rendered or sent to the warehouse.
    execution_context = replace(context, organization_id=version.organization_id)
    dependency_table_ids = await resolve_table_ids(
        session, datasource=datasource, table_names=version.referenced_tables
    )
    dependency_incidents = await fetch_open_incidents(
        session, datasource=datasource, table_ids=list(dependency_table_ids.values())
    )
    # R11-FP16: a tool generated from a view or routine also depends on that source's definition.
    source_asset_ids, source_holds = await fetch_source_binding_holds(session, version)
    quality_gate = check_tool_gate(
        tool_id=str(tool.id),
        dependency_asset_ids=[
            *(str(table_id) for table_id in dependency_table_ids.values()),
            *source_asset_ids,
        ],
        incidents=[*dependency_incidents, *source_holds],
    )
    if quality_gate.action == "BLOCK":
        message = (
            f"{quality_gate.message} {SOURCE_CHANGED_MESSAGE}"
            if source_holds
            else quality_gate.message
        )
        record_audit(
            session,
            execution_context,
            action="tool.execute",
            resource_type="governed_tool_version",
            resource_id=str(version.id),
            outcome="DENIED",
            correlation_id=get_correlation_id(),
            details={
                "reason": "QUALITY_INCIDENT_BLOCK",
                "message": message,
                "affected_assets": quality_gate.affected_assets,
                "source_definition_changed": bool(source_holds),
            },
        )
        await session.commit()
        raise HTTPException(status_code=409, detail=message)

    try:
        rendered = render_tool_sql(
            version.sql_template,
            dialect=datasource.dialect,
            definitions=[
                ToolParameterDefinition.model_validate(value) for value in version.parameter_schema
            ],
            values=body.parameters,
        )
    except ToolParameterError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    parameter_fingerprint = await sign_value(
        settings,
        json.dumps(rendered.normalized_parameters, sort_keys=True, separators=(",", ":")),
    )
    tool_execution = ToolExecution(
        **({"id": tool_execution_id} if tool_execution_id is not None else {}),
        organization_id=version.organization_id,
        tool_version_id=version.id,
        principal_id=context.principal_id,
        parameter_fingerprint=parameter_fingerprint,
    )
    session.add(tool_execution)
    await session.flush()
    semantic_version = (
        f"semantic-model:{version.semantic_model_version_id}"
        if version.semantic_model_version_id
        else None
    )
    gateway = QueryExecutionGateway(settings)
    try:
        result = await gateway.execute(
            session,
            datasource=datasource,
            context=execution_context,
            correlation_id=get_correlation_id(),
            sql=rendered.sql,
            requested_limit=body.max_rows,
            semantic_version=semantic_version,
            context_product_scope=context_product_scope,
        )
    except QueryRejected as exc:
        tool_execution.status = "REJECTED"
        tool_execution.query_execution_id = exc.execution_id
        tool_execution.error_message = str(exc)[:1000]
        await session.commit()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        tool_execution.status = "FAILED"
        tool_execution.error_message = "tool query execution failed"
        await session.commit()
        raise HTTPException(status_code=502, detail="tool execution failed") from exc
    tool_execution.status = "COMPLETED"
    tool_execution.query_execution_id = result.execution.id
    record_audit(
        session,
        execution_context,
        action="tool.execute",
        resource_type="tool_execution",
        resource_id=str(tool_execution.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "tool_version_id": str(version.id),
            "query_execution_id": str(result.execution.id),
            "quality_gate_action": quality_gate.action,
        },
    )
    record_outbox(
        session,
        organization_id=version.organization_id,
        aggregate_type="tool_execution",
        aggregate_id=str(tool_execution.id),
        event_type="tool.execution.completed.v1",
        payload={
            "tool_execution_id": str(tool_execution.id),
            "tool_version_id": str(version.id),
            "query_execution_id": str(result.execution.id),
        },
    )
    await session.commit()
    return ToolExecutionResponse(
        tool_execution_id=tool_execution.id,
        tool_version_id=version.id,
        tool_slug=tool.slug,
        tool_version=version.version,
        execution=query_execution_response(result),
        quality_gate=(
            {
                "action": quality_gate.action,
                "affected_assets": quality_gate.affected_assets,
                "message": quality_gate.message,
            }
            if quality_gate.action != "ALLOW"
            else None
        ),
    )
