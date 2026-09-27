"""ADR-0029: the quality agent's HTTP surface.

The same two endpoints every task agent has, with every task agent's response
shapes (`task_agent_api`). What the agent does lives in `aida.quality_agent`;
this module owns only the paths, the roles and the request.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from aida.db import get_session
from aida.quality_agent import QUALITY_AGENT, QUALITY_WORK
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

#: Who may start a run: exactly the roles that may create a quality rule by hand
#: (`quality_api.create_rule`). A run only proposes, so starting one lets them do
#: less than they already can.
QUALITY_AGENT_OPERATORS = ("PlatformAdmin", "DataAdmin", "DataSteward", "Operations")

QualityCapability = Literal["ROW_COUNT_FLOOR", "NULL_RATE_CEILING"]


def _every_capability() -> list[QualityCapability]:
    return ["ROW_COUNT_FLOOR", "NULL_RATE_CEILING"]


class QualityAgentRunRequest(ApiModel):
    capabilities: list[QualityCapability] = Field(
        default_factory=_every_capability, min_length=1, max_length=2
    )
    #: Proposals per capability, clamped server-side to
    #: `quality_agent_max_proposals_per_run`.
    limit: int = Field(default=10, ge=1, le=200)
    datasource_id: UUID | None = None
    #: Report what the agent would propose and open nothing, whatever its tier.
    dry_run: bool = False

    @model_validator(mode="after")
    def _unique_capabilities(self) -> QualityAgentRunRequest:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        return self


@router.get(
    "/organizations/{organization_id}/quality-agent", response_model=TaskAgentStateRead
)
async def get_quality_agent_state(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*TASK_AGENT_READERS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TaskAgentStateRead:
    enforce_organization(context, organization_id)
    state = await task_agent_status(
        session, organization_id, spec=QUALITY_AGENT, settings=settings
    )
    return task_agent_state_read(organization_id, state, QUALITY_AGENT, settings)


@router.post(
    "/organizations/{organization_id}/quality-agent/run", response_model=TaskAgentRunRead
)
async def start_quality_agent_run(
    organization_id: UUID,
    body: QualityAgentRunRequest,
    context: SecurityContext = Depends(require_roles(*QUALITY_AGENT_OPERATORS)),
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
        spec=QUALITY_AGENT,
        work=QUALITY_WORK,
        request=TaskAgentRunRequest(
            capabilities=tuple(body.capabilities),
            limit=body.limit,
            datasource_id=body.datasource_id,
            dry_run=body.dry_run,
        ),
        settings=settings,
        context=context,
    )
