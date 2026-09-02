"""UX-19: `GET /v1/organizations/{organization_id}/ai-agents/roster`.

See `aida.agent_roster` for how the response is composed, and in particular
its module docstring for the two honesty notes this row's exit condition
requires: the organization-wide (not per-agent) scope of the method/results
data, and why every agent is reported with no auto-apply branch.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from aida.agent_roster import (
    DEFAULT_METHOD_SAMPLE_LIMIT,
    DEFAULT_RECENT_RESULTS_LIMIT,
    MAX_RECENT_RESULTS_LIMIT,
    AgentRosterRead,
    compose_agent_roster,
)
from aida.ai_registry_api import AI_READERS
from aida.db import get_session
from aida.security import SecurityContext, enforce_organization, require_roles
from aida.tool_first_rate import DEFAULT_WINDOW_DAYS

router = APIRouter(prefix="/v1", tags=["ai-agent-roster"])


@router.get(
    "/organizations/{organization_id}/ai-agents/roster",
    response_model=AgentRosterRead,
)
async def get_agent_roster(
    organization_id: UUID,
    window_days: int = Query(default=DEFAULT_WINDOW_DAYS, ge=1, le=365),
    recent_results_limit: int = Query(
        default=DEFAULT_RECENT_RESULTS_LIMIT, ge=1, le=MAX_RECENT_RESULTS_LIMIT
    ),
    context: SecurityContext = Depends(require_roles(*AI_READERS)),
    session: AsyncSession = Depends(get_session),
) -> AgentRosterRead:
    """The agent roster: for every registered `AGENT`-kind AI asset in this
    organization, its published purpose (EA.10c AI registry), an aggregated
    method summary (recent `AgentRun.plan_evidence`/`generation_source`,
    reusing TL-6's `tool_first_execution_rate` verbatim), a bounded window
    of recent live results, and an honest auto-apply determination -- so a
    steward can inspect an agent's method before trusting its output.

    Same authorization boundary as the rest of the AI registry
    (`aida.ai_registry_api.AI_READERS`): this is a read over the same
    registry data, plus derived run evidence, for the same audience.
    """
    enforce_organization(context, organization_id)
    return await compose_agent_roster(
        session,
        organization_id=organization_id,
        window_days=window_days,
        recent_results_limit=recent_results_limit,
        method_sample_limit=DEFAULT_METHOD_SAMPLE_LIMIT,
    )
