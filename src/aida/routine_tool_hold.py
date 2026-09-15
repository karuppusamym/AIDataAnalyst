"""R11-FP16: a governed tool extracted from a routine is held once that routine changes.

A procedure tool (`procedure_tool_blueprint`) is the routine's one result query, copied out of
the routine body when the tool was generated. When the source redefines or retires the routine,
the copy may no longer answer what the routine does, and nothing held it: the holds
`change_signal_processing` places are on the tables a tool reads, not on the code it was lifted
from.

The hold is read from change signals rather than stored. A version whose source routine has a
DEFINITION_CHANGED or DEPRECATED signal detected after the version was approved is blocked on
both execution paths, through the same `check_tool_gate` a critical table incident uses. It lifts
without anyone resolving it once a version generated after the change is approved -- the
re-verification the hold exists to ask for.
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import SIGNAL_DEFINITION_CHANGED, SIGNAL_DEPRECATED
from aida.models import GovernedToolVersion
from aida.quality_coupling import IncidentSummary

SOURCE_ROUTINE_CHANGE_ANOMALY_TYPE: Final = "SOURCE_ROUTINE_CHANGE"
SOURCE_ROUTINE_CHANGED_MESSAGE: Final = (
    "The routine this tool was extracted from was redefined or retired after the tool was "
    "approved. Approve a version generated from its current definition."
)


async def fetch_source_routine_holds(
    session: AsyncSession, version: GovernedToolVersion
) -> tuple[list[str], list[IncidentSummary]]:
    """The version's source routine as a dependency, with a CRITICAL hold if it changed since
    the version was approved. Both empty for a tool not extracted from a routine."""
    routine_id = version.source_routine_id
    if routine_id is None:
        return [], []
    since = version.approved_at or version.created_at
    signal = await session.scalar(
        select(MetadataChangeSignal)
        .where(
            MetadataChangeSignal.organization_id == version.organization_id,
            MetadataChangeSignal.subject_kind == "ROUTINE",
            MetadataChangeSignal.subject_id == routine_id,
            MetadataChangeSignal.signal_type.in_((SIGNAL_DEFINITION_CHANGED, SIGNAL_DEPRECATED)),
            MetadataChangeSignal.detected_at > since,
        )
        .order_by(MetadataChangeSignal.detected_at.desc())
        .limit(1)
    )
    asset_id = str(routine_id)
    if signal is None:
        return [asset_id], []
    return [asset_id], [
        IncidentSummary(
            incident_id=str(signal.id),
            asset_id=asset_id,
            severity="CRITICAL",
            status="OPEN",
            anomaly_type=SOURCE_ROUTINE_CHANGE_ANOMALY_TYPE,
        )
    ]
