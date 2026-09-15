"""R11-FP16: act on change signals -- hold what a source change can have broken, nothing else.

R11-FP15 records *which* objects changed. This consumes those records, one PENDING signal at a
time, and routes each through machinery that already exists rather than a second lifecycle
(R11-S4 freezes new governed-artifact families):

* **A view redefined in any way, or a table retired**, opens (or reopens) one CRITICAL incident
  on that table in the shared data-quality incident sink. DQ-3's coupling already fails every
  governed tool that depends on the table closed (`quality_coupling.check_tool_gate`, on both
  the REST and the agent execution path), and a context product that denies on critical
  incidents stops serving. A literal-only redefinition counts: `status = 'OPEN'` becoming
  `status = 'CLOSED'` leaves lineage intact and changes every answer. A steward resolves the
  incident once the dependants are checked -- the same act that clears any other incident --
  or `context_rebuild` resolves it once nothing standing on the table is stale.
* **A table that changed shape, or a view whose definition the source stopped providing**, opens
  a WARNING: tools still run, and say why they might be wrong.
* **A view or routine redefined, retired or returning** is left to the lineage agent, which
  re-examines any definition changed structurally since its newest edge (`lineage_agent`).
* Permission and meaning signals are recorded as seen; nothing downstream consumes them yet.

Every signal ends PROCESSED with the action taken in `outcome` -- the per-signal watermark. Each is
applied in its own savepoint, so one failure leaves the rest of the batch intact and the failed
signal PENDING for the next pass. Value-free throughout: ids and codes.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.change_signal_models import MetadataChangeSignal
from aida.change_signals import (
    SIGNAL_DEFINITION_CHANGED,
    SIGNAL_DEPRECATED,
    SIGNAL_REACTIVATED,
    SIGNAL_STRUCTURE_CHANGED,
)
from aida.events import record_audit
from aida.models import DataQualityIncident, MetadataTable
from aida.security import SecurityContext

logger = structlog.get_logger(__name__)

CHANGE_SIGNAL_PROCESSOR_PRINCIPAL: Final = "scheduler:change-signal-processor"
#: The incident sink's anomaly type for every hold this module places.
SOURCE_CHANGE_ANOMALY_TYPE: Final = "SOURCE_CHANGE"

ACTION_VIEW_REDEFINED: Final = "VIEW_REDEFINED"
ACTION_TABLE_RETIRED: Final = "TABLE_RETIRED"
ACTION_TABLE_RESHAPED: Final = "TABLE_RESHAPED"
ACTION_VIEW_DEFINITION_RETIRED: Final = "VIEW_DEFINITION_RETIRED"
ACTION_LINEAGE_REEXAMINE: Final = "LINEAGE_REEXAMINE"
ACTION_RECORDED: Final = "RECORDED"
ACTION_SUBJECT_GONE: Final = "SUBJECT_GONE"

_SEVERITY_RANK: Final = {"WARNING": 1, "CRITICAL": 2}
_EVIDENCE_LIMIT: Final = 20
_SUMMARIES: Final = {
    ACTION_VIEW_REDEFINED: (
        "The source redefined this view. Governed tools over it are held until a steward "
        "confirms they still answer what was approved."
    ),
    ACTION_TABLE_RETIRED: (
        "This table left the source's snapshot. Governed tools over it are held until a "
        "steward reviews them."
    ),
    ACTION_TABLE_RESHAPED: (
        "This table's columns changed in the source. Tools over it still run and may need review."
    ),
    ACTION_VIEW_DEFINITION_RETIRED: (
        "The source stopped providing this view's definition. Its lineage can no longer be "
        "checked against the source."
    ),
}


def decide(signal: MetadataChangeSignal) -> tuple[str, str | None]:
    """The action a signal calls for, and the incident severity it opens (None for none)."""
    kind, signal_type = signal.subject_kind, signal.signal_type
    if kind == "VIEW" and signal_type == SIGNAL_DEFINITION_CHANGED:
        return ACTION_VIEW_REDEFINED, "CRITICAL"
    if kind == "TABLE" and signal_type == SIGNAL_DEPRECATED:
        return ACTION_TABLE_RETIRED, "CRITICAL"
    if kind == "TABLE" and signal_type == SIGNAL_STRUCTURE_CHANGED:
        return ACTION_TABLE_RESHAPED, "WARNING"
    if kind == "VIEW" and signal_type == SIGNAL_DEPRECATED:
        return ACTION_VIEW_DEFINITION_RETIRED, "WARNING"
    if kind in ("VIEW", "ROUTINE") and signal_type in (
        SIGNAL_DEFINITION_CHANGED,
        SIGNAL_REACTIVATED,
        SIGNAL_DEPRECATED,
    ):
        return ACTION_LINEAGE_REEXAMINE, None
    return ACTION_RECORDED, None


def source_change_fingerprint(organization_id: UUID, table_id: UUID) -> str:
    """One incident per table: a second change updates it rather than stacking a new one."""
    return hashlib.sha256(f"{organization_id}:{table_id}:source_change".encode()).hexdigest()


@dataclass(slots=True)
class ProcessingOutcome:
    processed: int = 0
    failed: int = 0
    incidents_opened: int = 0
    incidents_updated: int = 0
    actions: dict[str, int] = field(default_factory=dict)

    def as_details(self) -> dict[str, Any]:
        return {
            "processed": self.processed,
            "failed": self.failed,
            "incidents_opened": self.incidents_opened,
            "incidents_updated": self.incidents_updated,
            "actions": dict(sorted(self.actions.items())),
        }


async def _hold(
    session: AsyncSession, signal: MetadataChangeSignal, action: str, severity: str, now: datetime
) -> tuple[str, UUID | None, bool | None]:
    table = await session.get(MetadataTable, signal.subject_id)
    if table is None or table.organization_id != signal.organization_id:
        return ACTION_SUBJECT_GONE, None, None
    fingerprint = source_change_fingerprint(signal.organization_id, table.id)
    change = {"signal_id": str(signal.id), "action": action}
    if signal.change_class is not None:
        # R11-FP16: the kind of change, so a rebuild can tell one a bound query survives.
        change["change_class"] = signal.change_class
    incident = await session.scalar(
        select(DataQualityIncident).where(DataQualityIncident.fingerprint == fingerprint)
    )
    if incident is None:
        incident = DataQualityIncident(
            organization_id=signal.organization_id,
            datasource_id=table.datasource_id,
            table_id=table.id,
            policy_id=None,
            fingerprint=fingerprint,
            anomaly_type=SOURCE_CHANGE_ANOMALY_TYPE,
            severity=severity,
            status="OPEN",
            summary=_SUMMARIES[action],
            evidence={"changes": [change]},
            first_observed_at=now,
            last_observed_at=now,
        )
        session.add(incident)
        await session.flush()
        return action, incident.id, True
    reopened = incident.status == "RESOLVED"
    # A new change is new information: an acknowledged hold asks for attention again. Severity
    # only rises while the hold is open; a resolved one starts from this change.
    if reopened or _SEVERITY_RANK[severity] >= _SEVERITY_RANK.get(incident.severity, 0):
        incident.severity = severity
        incident.summary = _SUMMARIES[action]
    incident.status = "OPEN"
    previous = list((incident.evidence or {}).get("changes") or [])
    incident.evidence = {"changes": [*previous[-(_EVIDENCE_LIMIT - 1) :], change]}
    incident.occurrence_count += 1
    incident.last_observed_at = now
    if reopened:
        incident.resolved_by = None
        incident.resolved_at = None
        incident.resolution_reason = None
    return action, incident.id, reopened


async def process_change_signals(
    session: AsyncSession,
    *,
    organization_id: UUID,
    limit: int,
    now: datetime | None = None,
) -> ProcessingOutcome:
    """Process one organization's oldest PENDING signals, at most `limit`. The caller commits."""
    effective_now = now or datetime.now(UTC)
    signals = (
        await session.scalars(
            select(MetadataChangeSignal)
            .where(
                MetadataChangeSignal.organization_id == organization_id,
                MetadataChangeSignal.status == "PENDING",
            )
            .order_by(MetadataChangeSignal.detected_at, MetadataChangeSignal.id)
            .limit(limit)
        )
    ).all()
    outcome = ProcessingOutcome()
    for signal in signals:
        try:
            async with session.begin_nested():
                action, severity = decide(signal)
                incident_id: UUID | None = None
                opened: bool | None = None
                if severity is not None:
                    action, incident_id, opened = await _hold(
                        session, signal, action, severity, effective_now
                    )
                signal.status = "PROCESSED"
                signal.processed_at = effective_now
                signal.processed_by = CHANGE_SIGNAL_PROCESSOR_PRINCIPAL
                signal.outcome = {
                    "action": action,
                    **({"incident_id": str(incident_id)} if incident_id is not None else {}),
                }
        except Exception:  # noqa: BLE001 -- one signal must not stop the batch
            logger.exception("change_signal_processing_item_failed", signal_id=str(signal.id))
            outcome.failed += 1
            continue
        outcome.processed += 1
        outcome.actions[action] = outcome.actions.get(action, 0) + 1
        if opened is True:
            outcome.incidents_opened += 1
        elif opened is False:
            outcome.incidents_updated += 1
    if signals:
        record_audit(
            session,
            SecurityContext(
                principal_id=CHANGE_SIGNAL_PROCESSOR_PRINCIPAL,
                principal_type="WORKER",
                organization_id=organization_id,
                roles=frozenset({"SchedulerWorker"}),
            ),
            action="metadata.change_signals.process",
            resource_type="organization",
            resource_id=str(organization_id),
            outcome="SUCCESS",
            correlation_id=str(organization_id),
            details=outcome.as_details(),
        )
    return outcome
