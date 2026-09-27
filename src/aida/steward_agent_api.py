"""ADR-0029: the steward agent's HTTP surface.

Two endpoints: the agent's state -- is it registered here, what may it do, is
anything stopping it, how have its proposals fared -- and a run. The response
shapes are every task agent's (`task_agent_api`); what the agent does lives in
`aida.steward_agent`; this module owns only the paths, the roles and the
request.
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
from aida.steward_agent import STEWARD_AGENT, STEWARD_WORK
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

#: Who may start a run: exactly the roles that may already generate these
#: drafts and link proposals by hand (`asset_description_api.WRITE_ROLES`,
#: `stewardship_api.WRITE_ROLES`). Starting the agent grants no new power -- it
#: is the same drafting, attributed to the agent and bounded by its contract.
STEWARD_AGENT_OPERATORS = ("PlatformAdmin", "MetadataAdmin", "SemanticAdmin", "DataSteward")

StewardCapability = Literal["TABLE_DESCRIPTION", "COLUMN_DESCRIPTION", "GLOSSARY_LINK"]


def _every_capability() -> list[StewardCapability]:
    return ["TABLE_DESCRIPTION", "COLUMN_DESCRIPTION", "GLOSSARY_LINK"]


class StewardAgentRunRequest(ApiModel):
    capabilities: list[StewardCapability] = Field(
        default_factory=_every_capability, min_length=1, max_length=3
    )
    #: Proposals per capability, clamped server-side to
    #: `steward_agent_max_proposals_per_run`.
    limit: int = Field(default=10, ge=1, le=200)
    datasource_id: UUID | None = None
    #: Report what the agent would propose and open nothing, whatever its tier.
    dry_run: bool = False

    @model_validator(mode="after")
    def _unique_capabilities(self) -> StewardAgentRunRequest:
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("capabilities must be unique")
        return self


@router.get(
    "/organizations/{organization_id}/steward-agent", response_model=TaskAgentStateRead
)
async def get_steward_agent_state(
    organization_id: UUID,
    context: SecurityContext = Depends(require_roles(*TASK_AGENT_READERS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TaskAgentStateRead:
    enforce_organization(context, organization_id)
    state = await task_agent_status(
        session, organization_id, spec=STEWARD_AGENT, settings=settings
    )
    return task_agent_state_read(organization_id, state, STEWARD_AGENT, settings)


@router.post(
    "/organizations/{organization_id}/steward-agent/run", response_model=TaskAgentRunRead
)
async def start_steward_agent_run(
    organization_id: UUID,
    body: StewardAgentRunRequest,
    context: SecurityContext = Depends(require_roles(*STEWARD_AGENT_OPERATORS)),
    session: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> TaskAgentRunRead:
    """Run the agent once, synchronously and bounded.

    409 with a stable reason code when the agent may not act -- no contract, no
    approved version, a kill switch, or authority withdrawn mid-run -- and in
    that case nothing the run produced is kept.
    """
    enforce_organization(context, organization_id)  # INV-5: before the body
    await require_datasource_in_organization(session, organization_id, body.datasource_id)
    return await execute_task_agent_run(
        session,
        organization_id,
        spec=STEWARD_AGENT,
        work=STEWARD_WORK,
        request=TaskAgentRunRequest(
            capabilities=tuple(body.capabilities),
            limit=body.limit,
            datasource_id=body.datasource_id,
            dry_run=body.dry_run,
        ),
        settings=settings,
        context=context,
    )
