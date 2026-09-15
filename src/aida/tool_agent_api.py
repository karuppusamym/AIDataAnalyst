"""ADR-0029 / R11-FP14: the tool agent's HTTP surface.

The same two endpoints every task agent has, with every task agent's response shapes
(`task_agent_api`). What the agent does lives in `aida.tool_agent`; this module owns only the
paths, the roles and the request.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from aida.db import get_session
from aida.schemas import ApiModel
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.task_agent import TaskAgentRunRequest, task_agent_status
from aida.task_agent_api import (
    TASK_AGENT_READERS,
    TaskAgentRunRead,
    TaskAgentStateRead,
    execute_task_agent_run,
    require_datasource_in_organization,
    task_agent_state_read,
)
from aida.tool_agent import TOOL_AGENT, TOOL_WORK
from atlas.platform.config import Settings, get_settings

router = APIRouter(prefix="/v1", tags=["agent-workforce"])

#: Who may start a run: exactly the roles that may generate a tool blueprint by hand
#: (`tool_api.create_view_tool_blueprint`, `procedure_tool_api`). A run only drafts and asks
#: for review, so starting one lets them do less than they already can.
TOOL_AGENT_OPERATORS = ("PlatformAdmin", "ToolDeveloper", "SemanticAdmin")

ToolCapability = Literal["VIEW_TOOL", "PROCEDURE_TOOL"]


def _every_capability() -> list[ToolCapability]:
    return ["VIEW_TOOL", "PROCEDURE_TOOL"]


class ToolAgentRunRequest(ApiModel):
    capabilities: list[ToolCapability] = Field(
        default_factory=_every_capability, min_length=1, max_length=2
    )
    #: Proposals per capability, clamped server-side to `tool_agent_max_proposals_per_run`.
    limit: int = Field(default=10, ge=1, le=200)
    datasource_id: UUID | None = None
    #: Report what the agent would propose and open nothing, whatever its tier.
    dry_run: bool = False

    @model_validator(mode="after")
    def _unique_capabilities(self) -> ToolAgentRunRequest:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        return self


@router.get("/organizations/{organization_id}/tool-agent", response_model=TaskAgentStateRead)
async def get_tool_agent_state(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*TASK_AGENT_READERS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TaskAgentStateRead:
    enforce_organization(context, organization_id)
    state = await task_agent_status(session, organization_id, spec=TOOL_AGENT, settings=settings)
    return task_agent_state_read(organization_id, state, TOOL_AGENT, settings)


@router.post(
    "/organizations/{organization_id}/tool-agent/run", response_model=TaskAgentRunRead
)
async def start_tool_agent_run(
    organization_id: UUID,
    body: ToolAgentRunRequest,
    context: SecurityContext = Depends(require_roles(*TOOL_AGENT_OPERATORS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TaskAgentRunRead:
    """Run the agent once, synchronously and bounded; 409 and nothing kept when it may not act
    -- including an edition without tool authoring (`agent_capability_not_entitled`)."""
    enforce_organization(context, organization_id)  # INV-5: before the body
    await require_datasource_in_organization(session, organization_id, body.datasource_id)
    return await execute_task_agent_run(
        session,
        organization_id,
        spec=TOOL_AGENT,
        work=TOOL_WORK,
        request=TaskAgentRunRequest(
            capabilities=tuple(body.capabilities),
            limit=body.limit,
            datasource_id=body.datasource_id,
            dry_run=body.dry_run,
        ),
        settings=settings,
        context=context,
    )
