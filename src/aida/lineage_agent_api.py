"""ADR-0029: the lineage agent's HTTP surface.

The same two endpoints every task agent has, with every task agent's response
shapes (`task_agent_api`). What the agent does lives in `aida.lineage_agent`;
this module owns only the paths, the roles and the request.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from aida.db import get_session
from aida.lineage_agent import LINEAGE_AGENT, LINEAGE_WORK
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
from atlas.platform.config import Settings, get_settings

router = APIRouter(prefix="/v1", tags=["agent-workforce"])

#: Who may start a run: exactly the roles that may already parse a view or a
#: captured routine into lineage by hand (`_LINEAGE_WRITER_ROLES` in
#: `view_lineage_api` and `procedure_lineage_api`).
LINEAGE_AGENT_OPERATORS = ("PlatformAdmin", "MetadataAdmin", "DataAdmin", "DataSteward")

LineageCapability = Literal["VIEW_LINEAGE", "PROCEDURE_LINEAGE"]


def _every_capability() -> list[LineageCapability]:
    return ["VIEW_LINEAGE", "PROCEDURE_LINEAGE"]


class LineageAgentRunRequest(ApiModel):
    capabilities: list[LineageCapability] = Field(
        default_factory=_every_capability, min_length=1, max_length=2
    )
    #: Views, and routines, proposed from per capability, clamped server-side
    #: to `lineage_agent_max_proposals_per_run`.
    limit: int = Field(default=10, ge=1, le=200)
    datasource_id: UUID | None = None
    #: Report what the agent would propose and write nothing, whatever its tier.
    dry_run: bool = False


@router.get(
    "/organizations/{organization_id}/lineage-agent", response_model=TaskAgentStateRead
)
async def get_lineage_agent_state(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*TASK_AGENT_READERS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TaskAgentStateRead:
    enforce_organization(context, organization_id)
    state = await task_agent_status(
        session, organization_id, spec=LINEAGE_AGENT, settings=settings
    )
    return task_agent_state_read(organization_id, state, LINEAGE_AGENT, settings)


@router.post(
    "/organizations/{organization_id}/lineage-agent/run", response_model=TaskAgentRunRead
)
async def start_lineage_agent_run(
    organization_id: UUID,
    body: LineageAgentRunRequest,
    context: SecurityContext = Depends(require_roles(*LINEAGE_AGENT_OPERATORS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TaskAgentRunRead:
    """Run the agent once, synchronously and bounded; 409 and nothing kept
    when it may not act."""
    enforce_organization(context, organization_id)  # INV-5: before the body
    await require_datasource_in_organization(session, organization_id, body.datasource_id)
    return await execute_task_agent_run(
        session,
        organization_id,
        spec=LINEAGE_AGENT,
        work=LINEAGE_WORK,
        request=TaskAgentRunRequest(
            capabilities=tuple(body.capabilities),
            limit=body.limit,
            datasource_id=body.datasource_id,
            dry_run=body.dry_run,
        ),
        settings=settings,
        context=context,
    )
