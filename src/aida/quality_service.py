import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.data_quality import QualityProfile, evaluate_quality, normalized_policy
from aida.events import record_audit, record_outbox
from aida.models import (
    AnalysisRun,
    ColumnProfile,
    DataQualityIncident,
    DataQualityObservation,
    DataQualityPolicy,
    TableProfile,
)
from aida.security import SecurityContext


def _row_count(profile: TableProfile) -> int | None:
    return (
        profile.row_count_estimate
        if profile.row_count_estimate is not None
        else profile.sampled_row_count
    )


def _snapshot(profile: TableProfile, columns: list[ColumnProfile]) -> QualityProfile:
    rates: dict[str, float] = {}
    for column in columns:
        total = column.null_count + column.non_null_count
        rates[str(column.column_id)] = column.null_count / total if total else 0.0
    return QualityProfile(
        row_count=_row_count(profile),
        schema_fingerprint=profile.schema_fingerprint,
        null_rates=rates,
    )


def policy_snapshot(policy: DataQualityPolicy | None) -> dict[str, Any]:
    overrides = (
        {
            "volume_change_percent": policy.volume_change_percent,
            "null_rate_change_percent": policy.null_rate_change_percent,
            "schema_change_enabled": policy.schema_change_enabled,
            "metadata_scan_max_age_minutes": policy.metadata_scan_max_age_minutes,
        }
        if policy
        else None
    )
    return {**normalized_policy(overrides), "source": "CONFIGURED" if policy else "SYSTEM_DEFAULT"}


def _incident_fingerprint(organization_id: UUID, table_id: UUID, anomaly_type: str) -> str:
    material = f"{organization_id}:{table_id}:{anomaly_type}".encode()
    return hashlib.sha256(material).hexdigest()


async def evaluate_analysis_run(
    session: AsyncSession,
    *,
    analysis_run_id: UUID,
    organization_id: UUID,
    datasource_id: UUID,
    context: SecurityContext,
) -> dict[str, int]:
    """Persist idempotent observations and reconcile durable incident lifecycles."""
    # Serialize worker completion and operator replay for the same run.
    await session.execute(
        select(AnalysisRun.id).where(AnalysisRun.id == analysis_run_id).with_for_update()
    )
    profiles = (
        await session.scalars(
            select(TableProfile)
            .where(TableProfile.analysis_run_id == analysis_run_id)
            .order_by(TableProfile.table_id)
        )
    ).all()
    existing_table_ids = set(
        await session.scalars(
            select(DataQualityObservation.table_id).where(
                DataQualityObservation.analysis_run_id == analysis_run_id
            )
        )
    )
    profiles = [profile for profile in profiles if profile.table_id not in existing_table_ids]
    counts = {
        "observations": 0,
        "healthy": 0,
        "warning": 0,
        "critical": 0,
        "no_baseline": 0,
        "incidents_opened": 0,
        "incidents_resolved": 0,
    }
    if not profiles:
        return counts

    table_ids = [profile.table_id for profile in profiles]
    baseline_rank = (
        select(
            TableProfile,
            func.row_number()
            .over(
                partition_by=TableProfile.table_id,
                order_by=TableProfile.created_at.desc(),
            )
            .label("baseline_rank"),
        )
        .where(
            TableProfile.datasource_id == datasource_id,
            TableProfile.analysis_run_id != analysis_run_id,
            TableProfile.table_id.in_(table_ids),
        )
        .subquery()
    )
    baseline_alias = aliased(TableProfile, baseline_rank)
    baselines = (
        await session.scalars(select(baseline_alias).where(baseline_rank.c.baseline_rank == 1))
    ).all()
    baseline_by_table = {profile.table_id: profile for profile in baselines}
    all_profile_ids = [profile.id for profile in profiles] + [profile.id for profile in baselines]
    column_rows = (
        await session.scalars(
            select(ColumnProfile).where(ColumnProfile.table_profile_id.in_(all_profile_ids))
        )
    ).all()
    columns_by_profile: dict[UUID, list[ColumnProfile]] = {}
    for column in column_rows:
        columns_by_profile.setdefault(column.table_profile_id, []).append(column)
    policies = (
        await session.scalars(
            select(DataQualityPolicy).where(
                DataQualityPolicy.datasource_id == datasource_id,
                DataQualityPolicy.enabled.is_(True),
            )
        )
    ).all()
    default_policy = next((policy for policy in policies if policy.table_id is None), None)
    policy_by_table = {
        policy.table_id: policy for policy in policies if policy.table_id is not None
    }
    incidents = (
        await session.scalars(
            select(DataQualityIncident).where(DataQualityIncident.table_id.in_(table_ids))
        )
    ).all()
    incident_by_control = {
        (incident.table_id, incident.anomaly_type): incident for incident in incidents
    }
    active_by_table: dict[UUID, list[DataQualityIncident]] = {}
    for incident in incidents:
        if incident.status in {"OPEN", "ACKNOWLEDGED"}:
            active_by_table.setdefault(incident.table_id, []).append(incident)

    now = datetime.now(UTC)
    for profile in profiles:
        baseline = baseline_by_table.get(profile.table_id)
        policy = policy_by_table.get(profile.table_id) or default_policy
        snapshot = policy_snapshot(policy)
        result = evaluate_quality(
            _snapshot(profile, columns_by_profile.get(profile.id, [])),
            _snapshot(baseline, columns_by_profile.get(baseline.id, [])) if baseline else None,
            snapshot,
        )
        observation = DataQualityObservation(
            id=uuid4(),
            organization_id=organization_id,
            datasource_id=datasource_id,
            table_id=profile.table_id,
            analysis_run_id=analysis_run_id,
            baseline_profile_id=baseline.id if baseline else None,
            policy_id=policy.id if policy else None,
            status=result.status,
            quality_score=result.score,
            anomaly_types=list(result.anomaly_types),
            evidence=result.evidence,
            policy_snapshot=snapshot,
        )
        session.add(observation)
        counts["observations"] += 1
        counts[result.status.lower()] += 1

        active_incidents = active_by_table.get(profile.table_id, [])
        current_types = set(result.anomaly_types)
        if result.status != "NO_BASELINE":
            for incident in active_incidents:
                if incident.anomaly_type not in current_types:
                    incident.status = "RESOLVED"
                    incident.resolved_by = "quality-engine"
                    incident.resolved_at = now
                    incident.resolution_reason = "Control returned within the configured threshold."
                    counts["incidents_resolved"] += 1

        for anomaly_type in result.anomaly_types:
            fingerprint = _incident_fingerprint(organization_id, profile.table_id, anomaly_type)
            current_incident = incident_by_control.get((profile.table_id, anomaly_type))
            severity = result.severities[anomaly_type]
            anomaly_label = anomaly_type.replace("_", " ").lower()
            if current_incident is None:
                current_incident = DataQualityIncident(
                    id=uuid4(),
                    organization_id=organization_id,
                    datasource_id=datasource_id,
                    table_id=profile.table_id,
                    policy_id=policy.id if policy else None,
                    latest_observation_id=observation.id,
                    fingerprint=fingerprint,
                    anomaly_type=anomaly_type,
                    severity=severity,
                    summary=f"Detected {anomaly_label} outside the governed baseline threshold.",
                    evidence=result.evidence,
                    first_observed_at=now,
                    last_observed_at=now,
                )
                session.add(current_incident)
                incident_by_control[(profile.table_id, anomaly_type)] = current_incident
                counts["incidents_opened"] += 1
            else:
                reopened = current_incident.status == "RESOLVED"
                current_incident.latest_observation_id = observation.id
                current_incident.policy_id = policy.id if policy else None
                current_incident.severity = severity
                current_incident.status = "OPEN"
                current_incident.evidence = result.evidence
                current_incident.last_observed_at = now
                current_incident.occurrence_count += 1
                current_incident.resolved_by = None
                current_incident.resolved_at = None
                current_incident.resolution_reason = None
                if reopened:
                    counts["incidents_opened"] += 1

    record_audit(
        session,
        context,
        action="data_quality.analysis.evaluate",
        resource_type="analysis_run",
        resource_id=str(analysis_run_id),
        outcome="SUCCESS",
        correlation_id=str(analysis_run_id),
        details=counts,
    )
    record_outbox(
        session,
        organization_id=organization_id,
        aggregate_type="analysis_run",
        aggregate_id=str(analysis_run_id),
        event_type="data_quality.analysis.evaluated.v1",
        payload={
            "analysis_run_id": str(analysis_run_id),
            "datasource_id": str(datasource_id),
            **counts,
        },
    )
    return counts
