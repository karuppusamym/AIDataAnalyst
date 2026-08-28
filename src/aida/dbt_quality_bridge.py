"""
Atlas dbt Quality & Contract Drift Bridge
=========================================

Automates Data Quality Incident lifecycle and Data Contract verification
driven by dbt test results (`run_results.json`) and physical warehouse catalogs (`catalog.json`).
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aida.context import get_correlation_id
from aida.events import record_audit, record_outbox
from aida.models import (
    DataQualityIncident,
    DbtResource,
    MetadataTable,
)
from aida.security import SecurityContext


def infer_dbt_test_anomaly_type(test_name: str, unique_id: str) -> str:
    """Infer the governance anomaly category from a dbt test identifier."""
    ident = f"{test_name} {unique_id}".lower()
    if "not_null" in ident or "notnull" in ident:
        return "NOT_NULL_VIOLATION"
    if "unique" in ident:
        return "UNIQUENESS_BREACH"
    if "relationship" in ident or "foreign_key" in ident:
        return "RELATIONSHIP_BREACH"
    if "accepted_values" in ident or "accepted_value" in ident:
        return "ACCEPTED_VALUES_BREACH"
    if "freshness" in ident or "warn_after" in ident:
        return "LATENCY_SLA_BREACH"
    return "TRANSFORMATION_TEST_FAILURE"


def dbt_incident_fingerprint(
    organization_id: UUID, datasource_id: UUID, table_id: UUID, test_unique_id: str
) -> str:
    """Generate a deterministic fingerprint for an automated dbt test incident."""
    raw = f"{organization_id}:{datasource_id}:{table_id}:{test_unique_id}".encode()
    return hashlib.sha256(raw).hexdigest()


async def reconcile_dbt_test_quality(
    session: AsyncSession,
    *,
    organization_id: UUID,
    datasource_id: UUID,
    dbt_resources: list[DbtResource],
    context: SecurityContext,
) -> dict[str, int]:
    """
    Reconcile dbt test execution outcomes with durable Data Quality Incidents.

    - Failed/Error tests automatically open or reopen a DataQualityIncident.
    - Passing tests automatically resolve prior open incidents for that test.
    """
    now = datetime.now(UTC)
    counts = {
        "incidents_opened": 0,
        "incidents_reopened": 0,
        "incidents_resolved": 0,
    }

    # Index resources by unique_id
    resource_by_uid = {r.unique_id: r for r in dbt_resources}

    for resource in dbt_resources:
        if resource.resource_type != "TEST" or not resource.test_status:
            continue

        # Find the parent model or source table tested
        parent_table_id: UUID | None = None
        for dep_uid in resource.depends_on_unique_ids:
            parent = resource_by_uid.get(dep_uid)
            if parent and parent.matched_table_id:
                parent_table_id = parent.matched_table_id
                break

        if not parent_table_id:
            continue

        # Verify parent table exists
        parent_table = await session.get(MetadataTable, parent_table_id)
        if not parent_table:
            continue

        anomaly_type = infer_dbt_test_anomaly_type(resource.name, resource.unique_id)
        fingerprint = dbt_incident_fingerprint(
            organization_id, datasource_id, parent_table_id, resource.unique_id
        )

        existing_incident = await session.scalar(
            select(DataQualityIncident).where(DataQualityIncident.fingerprint == fingerprint)
        )

        if resource.test_status in {"FAIL", "ERROR"}:
            severity = "CRITICAL" if resource.test_status == "ERROR" else "WARNING"
            failures = resource.test_failures if resource.test_failures is not None else 1
            failure_suffix = "s" if failures != 1 else ""
            summary = f"dbt test failure: {resource.name} ({failures} failure{failure_suffix})"
            evidence: dict[str, Any] = {
                "source": "DBT_RUN_RESULTS",
                "test_unique_id": resource.unique_id,
                "test_name": resource.name,
                "status": resource.test_status,
                "failures": resource.test_failures,
                "execution_time": resource.test_execution_time,
                "anomaly_type": anomaly_type,
            }

            if existing_incident is None:
                incident = DataQualityIncident(
                    organization_id=organization_id,
                    datasource_id=datasource_id,
                    table_id=parent_table_id,
                    fingerprint=fingerprint,
                    anomaly_type=anomaly_type,
                    severity=severity,
                    status="OPEN",
                    summary=summary,
                    evidence=evidence,
                    occurrence_count=1,
                    first_observed_at=now,
                    last_observed_at=now,
                )
                session.add(incident)
                counts["incidents_opened"] += 1
                record_audit(
                    session,
                    context,
                    action="data_quality.incident.open",
                    resource_type="data_quality_incident",
                    resource_id=str(incident.id),
                    outcome="SUCCESS",
                    correlation_id=get_correlation_id(),
                    details={
                        "table_id": str(parent_table_id),
                        "test_unique_id": resource.unique_id,
                        "status": resource.test_status,
                    },
                )
                record_outbox(
                    session,
                    organization_id=organization_id,
                    aggregate_type="data_quality_incident",
                    aggregate_id=str(incident.id),
                    event_type="data_quality.incident_opened",
                    payload={
                        "incident_id": str(incident.id),
                        "datasource_id": str(datasource_id),
                        "table_id": str(parent_table_id),
                        "table_name": parent_table.name,
                        "anomaly_type": anomaly_type,
                        "severity": severity,
                        "summary": summary,
                    },
                )
            else:
                was_resolved = existing_incident.status == "RESOLVED"
                existing_incident.status = "OPEN"
                existing_incident.severity = severity
                existing_incident.summary = summary
                existing_incident.evidence = evidence
                existing_incident.occurrence_count += 1
                existing_incident.last_observed_at = now
                existing_incident.resolved_at = None
                existing_incident.resolved_by = None
                existing_incident.resolution_reason = None
                if was_resolved:
                    counts["incidents_reopened"] += 1
                else:
                    counts["incidents_opened"] += 1

        elif resource.test_status == "PASS":
            if existing_incident is not None and existing_incident.status == "OPEN":
                existing_incident.status = "RESOLVED"
                existing_incident.resolved_by = "SYSTEM_DBT_PASS"
                existing_incident.resolution_reason = (
                    f"dbt test '{resource.name}' passed in latest run_results ingestion."
                )
                existing_incident.resolved_at = now
                counts["incidents_resolved"] += 1
                record_audit(
                    session,
                    context,
                    action="data_quality.incident.resolve",
                    resource_type="data_quality_incident",
                    resource_id=str(existing_incident.id),
                    outcome="SUCCESS",
                    correlation_id=get_correlation_id(),
                    details={
                        "table_id": str(parent_table_id),
                        "test_unique_id": resource.unique_id,
                        "resolved_by": "SYSTEM_DBT_PASS",
                    },
                )
                record_outbox(
                    session,
                    organization_id=organization_id,
                    aggregate_type="data_quality_incident",
                    aggregate_id=str(existing_incident.id),
                    event_type="data_quality.incident_resolved",
                    payload={
                        "incident_id": str(existing_incident.id),
                        "datasource_id": str(datasource_id),
                        "table_id": str(parent_table_id),
                        "table_name": parent_table.name,
                        "resolved_by": "SYSTEM_DBT_PASS",
                    },
                )

    await session.flush()
    return counts
