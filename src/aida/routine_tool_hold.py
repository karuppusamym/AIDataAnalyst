"""R11-FP16: a governed tool extracted from a routine stands only while that routine does.

A procedure tool (`procedure_tool_blueprint`) is the routine's one result query, copied out of
the routine body when the draft was generated. The version records the routine and the
fingerprint of the definition the SQL was copied from (`source_routine_id`,
`source_definition_fingerprint`), and that binding is checked twice:

* **At approval** (`semantic_api._decide_governed_tool_version`): a draft whose routine has since
  been redefined, retired or removed is refused with `SOURCE_ROUTINE_MOVED`. When a version was
  approved says nothing about which definition its SQL came from: a draft generated before a
  change and approved after it would otherwise publish SQL copied from a definition that is gone.
* **At execution** (both paths, through `check_tool_gate`): a published version whose routine no
  longer matches its binding is blocked before any SQL is rendered.

The binding compares definitions, not times. A routine changed and then restored to exactly the
definition a version was generated from stands again; any other change needs a version generated
from the current definition, and approved. A version recorded before the fingerprint existed, and
not backfilled from the routine's definition history, falls back to change signals detected after
the version was generated -- never after it was approved.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import SIGNAL_DEFINITION_CHANGED, SIGNAL_DEPRECATED
from aida.envelope_models import AVAILABLE, MetadataRoutine
from aida.models import GovernedToolVersion
from aida.quality_coupling import IncidentSummary
from aida.refusal import RefusalDetail

SOURCE_ROUTINE_MOVED: Final = "SOURCE_ROUTINE_MOVED"
SOURCE_ROUTINE_CHANGE_ANOMALY_TYPE: Final = "SOURCE_ROUTINE_CHANGE"

#: Why a version no longer matches the routine it was generated from.
REASON_ROUTINE_MISSING: Final = "ROUTINE_MISSING"
REASON_ROUTINE_RETIRED: Final = "ROUTINE_RETIRED"
REASON_DEFINITION_CHANGED: Final = "DEFINITION_CHANGED"
REASON_CHANGED_SINCE_GENERATION: Final = "CHANGED_SINCE_GENERATION"

SOURCE_ROUTINE_CHANGED_MESSAGE: Final = (
    "The routine this tool was extracted from has been redefined, retired or removed since the "
    "tool's SQL was generated. Generate a version from its current definition and approve that."
)


async def source_routine_drift(session: AsyncSession, version: GovernedToolVersion) -> str | None:
    """Why `version` no longer matches its source routine, or `None` when it does (or has none)."""
    routine_id = version.source_routine_id
    if routine_id is None:
        return None
    routine = await session.get(MetadataRoutine, routine_id)
    if routine is None or routine.organization_id != version.organization_id:
        return REASON_ROUTINE_MISSING
    if routine.status != "ACTIVE":
        return REASON_ROUTINE_RETIRED
    bound = version.source_definition_fingerprint
    if bound is not None:
        if routine.availability != AVAILABLE or routine.body_fingerprint != bound:
            return REASON_DEFINITION_CHANGED
        return None
    changed = await session.scalar(
        select(MetadataChangeSignal.id)
        .where(
            MetadataChangeSignal.organization_id == version.organization_id,
            MetadataChangeSignal.subject_kind == "ROUTINE",
            MetadataChangeSignal.subject_id == routine_id,
            MetadataChangeSignal.signal_type.in_((SIGNAL_DEFINITION_CHANGED, SIGNAL_DEPRECATED)),
            MetadataChangeSignal.detected_at > version.created_at,
        )
        .limit(1)
    )
    return REASON_CHANGED_SINCE_GENERATION if changed is not None else None


def source_routine_refusal(reason: str) -> RefusalDetail:
    """The 409 body for approving a version whose source routine moved."""
    return RefusalDetail(
        code=SOURCE_ROUTINE_MOVED, reason=reason, message=SOURCE_ROUTINE_CHANGED_MESSAGE
    )


async def fetch_source_routine_holds(
    session: AsyncSession, version: GovernedToolVersion
) -> tuple[list[str], list[IncidentSummary]]:
    """The version's source routine as a dependency, with a CRITICAL hold when the version no
    longer matches it. Both empty for a tool not extracted from a routine."""
    if version.source_routine_id is None:
        return [], []
    asset_id = str(version.source_routine_id)
    reason = await source_routine_drift(session, version)
    if reason is None:
        return [asset_id], []
    return [asset_id], [
        IncidentSummary(
            incident_id=f"{SOURCE_ROUTINE_MOVED}:{reason}",
            asset_id=asset_id,
            severity="CRITICAL",
            status="OPEN",
            anomaly_type=SOURCE_ROUTINE_CHANGE_ANOMALY_TYPE,
        )
    ]
