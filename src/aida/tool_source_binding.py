"""R11-FP16: a governed tool generated from a view or routine stands only while that source does.

A view tool (`view_tool_blueprint`) reads a view's output columns as its captured definition
described them; a procedure tool (`procedure_tool_blueprint`) is a routine's one result query,
copied out of its body. Either way the tool's SQL was generated from one definition of one source
object. The version records which object (`source_view_table_id` or `source_routine_id`) and the
fingerprint of that definition (`source_definition_fingerprint`), and the binding is checked twice:

* **At approval** (`semantic_api._decide_governed_tool_version`): a draft whose source has since
  been redefined, retired or removed is refused with `SOURCE_DEFINITION_MOVED`. When a version
  was approved says nothing about which definition its SQL came from: a draft generated before a
  change and approved after it would otherwise publish SQL generated from a definition that is gone.
* **At execution** (both paths, through `check_tool_gate`): a version whose source no longer
  matches its binding is blocked before any SQL is rendered.

The binding compares definitions, not times. A source changed and then restored to exactly the
definition a version was generated from stands again; any other change needs a version generated
from the current definition, and approved (`context_rebuild` drafts one). A version recorded
before its fingerprint existed falls back to change signals detected after it was generated --
never after it was approved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import SIGNAL_DEFINITION_CHANGED, SIGNAL_DEPRECATED
from aida.envelope_models import AVAILABLE, MetadataRoutine, MetadataViewDefinition
from aida.models import GovernedToolVersion, MetadataTable
from aida.quality_coupling import IncidentSummary
from aida.refusal import RefusalDetail

SOURCE_DEFINITION_MOVED: Final = "SOURCE_DEFINITION_MOVED"
SOURCE_DEFINITION_CHANGE_ANOMALY_TYPE: Final = "SOURCE_DEFINITION_CHANGE"

SOURCE_KIND_VIEW: Final = "VIEW"
SOURCE_KIND_ROUTINE: Final = "ROUTINE"

#: Why a version no longer matches the source it was generated from.
REASON_SOURCE_MISSING: Final = "SOURCE_MISSING"
REASON_SOURCE_RETIRED: Final = "SOURCE_RETIRED"
REASON_DEFINITION_CHANGED: Final = "DEFINITION_CHANGED"
REASON_CHANGED_SINCE_GENERATION: Final = "CHANGED_SINCE_GENERATION"

SOURCE_CHANGED_MESSAGE: Final = (
    "The view or routine this tool was generated from has been redefined, retired or removed "
    "since the tool's SQL was generated. Generate a version from its current definition and "
    "approve that."
)


@dataclass(frozen=True, slots=True)
class SourceBinding:
    kind: str
    object_id: UUID


@dataclass(frozen=True, slots=True)
class SourceDefinition:
    """The bound source as it is now: whether it stands, and its definition's fingerprint."""

    reason: str | None
    fingerprint: str | None


def source_binding(version: GovernedToolVersion) -> SourceBinding | None:
    if version.source_routine_id is not None:
        return SourceBinding(SOURCE_KIND_ROUTINE, version.source_routine_id)
    if version.source_view_table_id is not None:
        return SourceBinding(SOURCE_KIND_VIEW, version.source_view_table_id)
    return None


async def current_source_definition(
    session: AsyncSession, organization_id: UUID, binding: SourceBinding
) -> SourceDefinition:
    """Whether the bound source still stands, and the fingerprint of its available definition."""
    if binding.kind == SOURCE_KIND_ROUTINE:
        routine = await session.get(MetadataRoutine, binding.object_id)
        if routine is None or routine.organization_id != organization_id:
            return SourceDefinition(REASON_SOURCE_MISSING, None)
        if routine.status != "ACTIVE":
            return SourceDefinition(REASON_SOURCE_RETIRED, None)
        available = routine.availability == AVAILABLE
        return SourceDefinition(None, routine.body_fingerprint if available else None)
    view = await session.get(MetadataTable, binding.object_id)
    if view is None or view.organization_id != organization_id:
        return SourceDefinition(REASON_SOURCE_MISSING, None)
    definition = await session.scalar(
        select(MetadataViewDefinition).where(MetadataViewDefinition.table_id == view.id)
    )
    if view.status != "ACTIVE" or definition is None or definition.status != "ACTIVE":
        return SourceDefinition(REASON_SOURCE_RETIRED, None)
    available = definition.availability == AVAILABLE
    return SourceDefinition(None, definition.definition_fingerprint if available else None)


async def source_binding_drift(session: AsyncSession, version: GovernedToolVersion) -> str | None:
    """Why `version` no longer matches its source, or `None` when it does (or has none)."""
    binding = source_binding(version)
    if binding is None:
        return None
    current = await current_source_definition(session, version.organization_id, binding)
    if current.reason is not None:
        return current.reason
    bound = version.source_definition_fingerprint
    if bound is not None:
        return None if current.fingerprint == bound else REASON_DEFINITION_CHANGED
    changed = await session.scalar(
        select(MetadataChangeSignal.id)
        .where(
            MetadataChangeSignal.organization_id == version.organization_id,
            MetadataChangeSignal.subject_kind == binding.kind,
            MetadataChangeSignal.subject_id == binding.object_id,
            MetadataChangeSignal.signal_type.in_((SIGNAL_DEFINITION_CHANGED, SIGNAL_DEPRECATED)),
            MetadataChangeSignal.detected_at > version.created_at,
        )
        .limit(1)
    )
    return REASON_CHANGED_SINCE_GENERATION if changed is not None else None


def source_binding_refusal(reason: str) -> RefusalDetail:
    """The 409 body for approving a version whose source moved."""
    return RefusalDetail(
        code=SOURCE_DEFINITION_MOVED, reason=reason, message=SOURCE_CHANGED_MESSAGE
    )


async def fetch_source_binding_holds(
    session: AsyncSession, version: GovernedToolVersion
) -> tuple[list[str], list[IncidentSummary]]:
    """The version's source as a dependency, with a CRITICAL hold when the version no longer
    matches it. Both empty for a tool not generated from a view or routine."""
    binding = source_binding(version)
    if binding is None:
        return [], []
    asset_id = str(binding.object_id)
    reason = await source_binding_drift(session, version)
    if reason is None:
        return [asset_id], []
    return [asset_id], [
        IncidentSummary(
            incident_id=f"{SOURCE_DEFINITION_MOVED}:{reason}",
            asset_id=asset_id,
            severity="CRITICAL",
            status="OPEN",
            anomaly_type=SOURCE_DEFINITION_CHANGE_ANOMALY_TYPE,
        )
    ]
