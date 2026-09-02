"""DQ-8: open framework for ingesting third-party detector quality signals.

Atlas deliberately does not compete on anomaly-detection science (module 11
``Docs/20-modules/11-data-quality.md`` §4 -- "ML anomaly detection: deliberately
not competed on -- integrate best-of-breed"). Its angle is *coupling*: a quality
incident should demote retrieval ranking, warn answers, and gate governed tools.
This module is the seam that lets a best-of-breed external detector (Monte Carlo,
Anomalo, ...) feed that same coupling by mapping a normalized detector signal onto
the existing ``DataQualityIncident`` lifecycle -- the identical durable,
fingerprinted, audited lifecycle Atlas's own controls
(``quality_service.evaluate_analysis_run``), custom rule packs
(``custom_quality_rules``) and the dbt bridge (``dbt_quality_bridge``) already use.

Two properties keep external and internal signals from ever being conflated:

* every ingested envelope is persisted as an immutable ``ExternalQualitySignal``
  row (its own table -- not mixed into ``DataQualityObservation``), and
* the ``DataQualityIncident`` it opens/reopens/resolves is stamped
  ``source="EXTERNAL"`` (internal detectors leave the column at its ``"INTERNAL"``
  default), with a vendor-namespaced ``anomaly_type`` and a fingerprint that
  includes the vendor + detector-native id.

Value-freedom (INV-6/ADR-0014): only detector metadata, refs and a normalized
severity/state are stored -- never source row values. The opaque ``details`` blob
is validated at the API boundary (``ExternalQualitySignalIngest``) to be a flat
metadata map, so nothing in this path becomes a value sink.

Idempotency: re-delivering the same detector event -- keyed on
``(organization_id, detector_vendor, detector_native_id, observed_at)`` -- returns
the already-stored signal without duplicating it or touching the incident again,
so at-least-once webhooks do not manufacture duplicate incident churn.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.events import record_audit, record_outbox
from aida.models import DataQualityIncident, ExternalQualitySignal
from aida.schemas import ExternalQualitySignalIngest
from aida.security import SecurityContext

EXTERNAL_SIGNAL_EVENT_TYPE = "data_quality.external_signal.ingested.v1"


@dataclass(frozen=True, slots=True)
class ExternalSignalIngestOutcome:
    signal: ExternalQualitySignal
    deduplicated: bool
    incident_opened: bool
    incident_resolved: bool


def external_incident_fingerprint(
    organization_id: UUID, datasource_id: UUID, table_id: UUID, vendor: str, native_id: str
) -> str:
    """Deterministic fingerprint for a third-party-detector incident.

    Includes vendor + detector-native id so two different monitors on the same
    table are two incidents, and re-detection of the *same* monitor reopens the
    one existing incident rather than duplicating it (module 11 §8).
    """
    material = (
        f"{organization_id}:{datasource_id}:{table_id}:external:{vendor}:{native_id}"
    ).encode()
    return hashlib.sha256(material).hexdigest()


def _anomaly_type(vendor: str) -> str:
    # Bounded to the ``anomaly_type`` column width (String(50)); the full
    # detector-native id lives on the ``ExternalQualitySignal`` row and in the
    # fingerprint, not here.
    return f"EXTERNAL:{vendor}"[:50]


def _incident_evidence(envelope: ExternalQualitySignalIngest) -> dict[str, object]:
    """Value-free evidence for the incident. Only detector metadata/refs -- the
    ``details`` blob was already validated flat-and-scalar at the API boundary."""
    return {
        "source": "EXTERNAL_DETECTOR",
        "detector_vendor": envelope.detector_vendor,
        "detector_native_id": envelope.detector_native_id,
        "signal_status": envelope.signal_status,
        "observed_at": envelope.observed_at.isoformat(),
        "details": dict(envelope.details),
    }


async def ingest_external_signal(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    envelope: ExternalQualitySignalIngest,
    context: SecurityContext,
) -> ExternalSignalIngestOutcome:
    """Persist one normalized external detector signal and reconcile it into the
    durable incident lifecycle. Audits and events in-transaction; the caller owns
    the commit (mirrors ``evaluate_rule_pack`` / ``reconcile_dbt_test_quality``).
    """
    vendor = envelope.detector_vendor
    native_id = envelope.detector_native_id

    existing_signal = await session.scalar(
        select(ExternalQualitySignal).where(
            ExternalQualitySignal.organization_id == organization_id,
            ExternalQualitySignal.detector_vendor == vendor,
            ExternalQualitySignal.detector_native_id == native_id,
            ExternalQualitySignal.observed_at == envelope.observed_at,
        )
    )
    if existing_signal is not None:
        # Idempotent replay: nothing new persisted, no incident churn. The audit
        # still records the attempt (INV-7); no domain event is re-emitted.
        record_audit(
            session,
            context,
            action="data_quality.external_signal.ingest",
            resource_type="external_quality_signal",
            resource_id=str(existing_signal.id),
            outcome="SUCCESS",
            correlation_id=get_correlation_id(),
            details={
                "detector_vendor": vendor,
                "detector_native_id": native_id,
                "deduplicated": True,
            },
        )
        return ExternalSignalIngestOutcome(
            signal=existing_signal,
            deduplicated=True,
            incident_opened=False,
            incident_resolved=False,
        )

    signal = ExternalQualitySignal(
        organization_id=organization_id,
        datasource_id=datasource_id,
        table_id=envelope.table_id,
        column_id=envelope.column_id,
        detector_vendor=vendor,
        detector_native_id=native_id,
        severity=envelope.severity,
        signal_status=envelope.signal_status,
        summary=envelope.summary,
        observed_at=envelope.observed_at,
        details=dict(envelope.details),
        created_by=context.principal_id,
    )
    session.add(signal)

    fingerprint = external_incident_fingerprint(
        organization_id, datasource_id, envelope.table_id, vendor, native_id
    )
    incident = await session.scalar(
        select(DataQualityIncident).where(DataQualityIncident.fingerprint == fingerprint)
    )
    evidence = _incident_evidence(envelope)
    observed_at = envelope.observed_at
    incident_opened = False
    incident_resolved = False

    if envelope.signal_status == "OPEN":
        if incident is None:
            incident = DataQualityIncident(
                organization_id=organization_id,
                datasource_id=datasource_id,
                table_id=envelope.table_id,
                fingerprint=fingerprint,
                anomaly_type=_anomaly_type(vendor),
                severity=envelope.severity,
                status="OPEN",
                source="EXTERNAL",
                summary=envelope.summary,
                evidence=evidence,
                first_observed_at=observed_at,
                last_observed_at=observed_at,
            )
            session.add(incident)
            incident_opened = True
        else:
            reopened = incident.status == "RESOLVED"
            incident.status = "OPEN"
            incident.severity = envelope.severity
            incident.source = "EXTERNAL"
            incident.summary = envelope.summary
            incident.evidence = evidence
            incident.last_observed_at = observed_at
            incident.occurrence_count += 1
            incident.resolved_by = None
            incident.resolved_at = None
            incident.resolution_reason = None
            incident_opened = reopened
    else:  # RESOLVED
        if incident is not None and incident.status != "RESOLVED":
            incident.status = "RESOLVED"
            incident.source = "EXTERNAL"
            incident.severity = envelope.severity
            incident.evidence = evidence
            incident.last_observed_at = observed_at
            incident.resolved_by = f"external-detector:{vendor}"
            incident.resolved_at = observed_at
            incident.resolution_reason = (
                f"{vendor} reported monitor {native_id} returned to normal."
            )
            incident_resolved = True

    await session.flush()
    if incident is not None:
        signal.incident_id = incident.id

    record_audit(
        session,
        context,
        action="data_quality.external_signal.ingest",
        resource_type="external_quality_signal",
        resource_id=str(signal.id),
        outcome="SUCCESS",
        correlation_id=get_correlation_id(),
        details={
            "detector_vendor": vendor,
            "detector_native_id": native_id,
            "signal_status": envelope.signal_status,
            "incident_id": str(incident.id) if incident is not None else None,
            "incident_opened": incident_opened,
            "incident_resolved": incident_resolved,
        },
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="external_quality_signal",
        aggregate_id=str(signal.id),
        event_type=EXTERNAL_SIGNAL_EVENT_TYPE,
        payload={
            "signal_id": str(signal.id),
            "datasource_id": str(datasource_id),
            "table_id": str(envelope.table_id),
            "detector_vendor": vendor,
            "detector_native_id": native_id,
            "severity": envelope.severity,
            "signal_status": envelope.signal_status,
            "incident_id": str(incident.id) if incident is not None else None,
            "incident_opened": incident_opened,
            "incident_resolved": incident_resolved,
        },
    )
    return ExternalSignalIngestOutcome(
        signal=signal,
        deduplicated=False,
        incident_opened=incident_opened,
        incident_resolved=incident_resolved,
    )
