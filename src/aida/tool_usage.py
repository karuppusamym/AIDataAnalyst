"""Usage-weighted tool ranking (TL-4).

Popular tools should rank higher wherever a consumer is offered a governed
tool to invoke -- the native ``GET /projects/{id}/tools`` listing
(`tool_api.py::list_tools`) and the MCP ``tools/list`` catalog
(`mcp_server.py::_handle_tools_list`, already filtered down to the caller's
role-eligible set by CX-5). This module supplies the one real,
already-persisted usage signal this codebase has for that ranking:
completed ``ToolExecution`` rows -- the same record
`tool_api.py::execute_tool` writes on every successful governed-tool
invocation, and the same source ST-A8's usage-derived eval mining reads
(via ``ConsumptionRecord`` for ``resource_type="governed_tool_version"``,
recorded on the MCP call path) to know a tool was actually used.

Usage is counted per *tool* (not per version) over a bounded lookback
window, so a newly-published version of a well-used tool inherits its
predecessor's popularity instead of starting back at zero -- the case that
matters most, since a maker publishing v2 of a heavily-used tool should not
see it fall to the bottom of the list the moment it ships.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.models import GovernedToolVersion, ToolExecution

DEFAULT_USAGE_LOOKBACK_DAYS = 90


async def get_tool_usage_counts(
    session: AsyncSession,
    *,
    organization_id: UUID | None,
    lookback_days: int = DEFAULT_USAGE_LOOKBACK_DAYS,
) -> dict[UUID, int]:
    """Return ``{tool_id: completed_execution_count}`` for this organization.

    Only ``COMPLETED`` executions count -- a ``REJECTED``/``FAILED`` attempt
    is not evidence the tool is useful. Absent from the returned mapping
    means zero, not unknown; callers should default-missing to 0.

    ``organization_id=None`` (an unauthenticated/organization-less
    `SecurityContext`, which every real caller of this function already
    handles as "nothing visible" for the same reason) returns ``{}`` rather
    than querying -- there is no real organization to count usage for.
    """
    if organization_id is None:
        return {}
    since = datetime.now(UTC) - timedelta(days=lookback_days)
    rows = (
        await session.execute(
            select(GovernedToolVersion.tool_id, func.count(ToolExecution.id))
            .join(ToolExecution, ToolExecution.tool_version_id == GovernedToolVersion.id)
            .where(
                GovernedToolVersion.organization_id == organization_id,
                ToolExecution.organization_id == organization_id,
                ToolExecution.status == "COMPLETED",
                ToolExecution.created_at >= since,
            )
            .group_by(GovernedToolVersion.tool_id)
        )
    ).all()
    return {tool_id: count for tool_id, count in rows}
