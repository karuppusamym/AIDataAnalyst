import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.config import Settings, get_settings
from aida.data_quality import QualityProfile, evaluate_quality, normalized_policy
from aida.events import record_audit, record_outbox
from aida.governance_notifications import notify_safely
from aida.models import (
    AnalysisRun,
    ColumnProfile,
    DataQualityIncident,
    DataQualityObservation,
    DataQualityPolicy,
    NotificationEventRecord,
    NotificationRuleRecord,
    TableProfile,
)
from aida.notification_routing import (
    Incident,
    NotificationRule,
    format_itsm_payload,
    route_notification,
)
from aida.quality_coupling import IncidentSummary, expire_sustained_incident_certifications
from aida.security import SecurityContext

# DQ-6: bounded per-table lookback for the day-of-week seasonal baseline query, only
# ever issued when `quality_seasonal_thresholds_enabled` is on (see `config.py`). At
# most this many of a table's most recent prior `TableProfile` rows are read to build
# its seasonal history, so the extra query stays cheap regardless of how long a table
# has been profiled -- roughly 17 weeks of daily scans, comfortably enough same-weekday
# points for `day_of_week_baseline`'s default `min_samples=3` well within a month.
# The same history also feeds the DQ-6 follow-up's month-end baseline
# (`quality_seasonal_month_end_enabled`) below -- both strategies read the one
# already-fetched list, so enabling both together adds no second query.
_SEASONALITY_HISTORY_LOOKBACK = 120


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


# --- GROUP C (DQ-1): quality-incident notification and ITSM webhook routing ---
#
# `evaluate_analysis_run` opens/reopens `DataQualityIncident` rows but, until
# now, never routed them anywhere -- DQ-1's engine (`notification_routing`)
# and its persistence tables (`NotificationRuleRecord`/`NotificationEventRecord`)
# existed and were reused by GL-6/KG-7 for *other* incident-shaped domains
# (unowned assets, graph drift), but no code path ever created a
# `NotificationEventRecord` for an actual data-quality incident, and no code
# path anywhere in the platform performed the outbound HTTP call an "ITSM
# webhook emitter" implies -- `siem_routing.route_to_siem` and
# `glossary_owner_routing`/`graph_reconciliation`'s ITSM handling both stop at
# formatting a payload and writing it to the outbox for an external consumer.
# This closes both gaps for quality incidents specifically: real
# `NotificationEventRecord` rows, and a real (optional, off-by-default)
# webhook POST for the ITSM channel, following `entitlements.apply_entitlement`'s
# httpx pattern.


def _as_notification_rules(rules: list[NotificationRuleRecord]) -> list[NotificationRule]:
    """Adapt persisted org notification rules into the pure engine's rule shape.

    Deliberately duplicated rather than imported -- the same trivial,
    module-private adapter `graph_reconciliation._as_engine_rules` and
    `glossary_owner_routing._as_engine_rules` each already own independently.
    """
    return [
        NotificationRule(
            rule_id=str(rule.id),
            organization_id=str(rule.organization_id),
            conditions=rule.conditions,
            channel=rule.channel,
            recipients=list(rule.recipients),
            escalation_after_minutes=rule.escalation_after_minutes,
            enabled=rule.enabled,
        )
        for rule in rules
    ]


def _incident_for_quality(incident: DataQualityIncident) -> Incident:
    """Build the engine's `Incident` shape from a persisted quality incident."""
    return Incident(
        incident_id=str(incident.id),
        fingerprint=incident.fingerprint,
        severity=incident.severity,
        source_id=str(incident.datasource_id),
        domain=None,
        owner=incident.acknowledged_by,
        message=incident.summary,
    )


async def emit_itsm_webhook(
    settings: Settings,
    payload: dict[str, Any],
    *,
    idempotency_key: str,
) -> tuple[str, str | None]:
    """POST an ITSM-formatted incident payload to the configured webhook URL.

    Returns ``(status, error)`` where ``status`` is ``"SENT"`` or
    ``"FAILED"``. Never raises -- a downed/misconfigured ITSM endpoint must
    not fail the quality-evaluation transaction that triggered it. Disabled
    (or unconfigured) deployments return ``"FAILED"`` with an explanatory
    error rather than silently pretending delivery succeeded, so a caller can
    tell "not configured" apart from "delivered" in the persisted event.
    """
    if not settings.dq_itsm_webhook_enabled:
        return "FAILED", "dq_itsm_webhook_enabled is off"
    if not settings.dq_itsm_webhook_url:
        return "FAILED", "dq_itsm_webhook_url is not configured"
    token = (
        settings.dq_itsm_webhook_token.get_secret_value()
        if settings.dq_itsm_webhook_token
        else None
    )
    headers = {"Idempotency-Key": idempotency_key, "Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(
            timeout=settings.dq_itsm_webhook_timeout_seconds, follow_redirects=False
        ) as client:
            response = await client.post(
                settings.dq_itsm_webhook_url, json=payload, headers=headers
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        return "FAILED", str(exc)[:1000]
    return "SENT", None


async def route_and_notify_incident(
    session: AsyncSession,
    incident: DataQualityIncident,
    *,
    organization_id: UUID,
    notification_rules: list[NotificationRuleRecord],
    settings: Settings | None = None,
) -> list[NotificationEventRecord]:
    """Route a newly-opened/reopened quality incident through DQ-1's engine.

    Matches ``incident`` against the organization's enabled notification
    rules, persists one `NotificationEventRecord` per match (so
    `GET /v1/notifications` and `POST /v1/notifications/{id}/acknowledge`
    -- previously dead code with no writer -- have real rows to act on), and
    for any ``ITSM``-channel match attempts the actual webhook POST,
    recording the outcome on the event's status rather than only on an
    outbox row.
    """
    if not notification_rules:
        return []
    settings = settings or get_settings()
    engine_rules = _as_notification_rules(notification_rules)
    rule_by_id = {str(rule.id): rule for rule in notification_rules}
    engine_incident = _incident_for_quality(incident)
    events = route_notification(engine_incident, engine_rules)

    now = datetime.now(UTC)
    created: list[NotificationEventRecord] = []
    for event in events:
        rule_row = rule_by_id.get(event.rule_id)
        if rule_row is None:
            continue
        record = NotificationEventRecord(
            id=uuid4(),
            organization_id=organization_id,
            incident_id=incident.id,
            rule_id=rule_row.id,
            channel=event.channel,
            recipients=list(event.recipients),
            status="PENDING",
            dedup_key=event.dedup_key,
        )
        if event.channel == "ITSM":
            payload = format_itsm_payload(engine_incident)
            status, error = await emit_itsm_webhook(
                settings, payload, idempotency_key=f"{incident.id}:{event.dedup_key}"
            )
            record.status = status
            if status == "SENT":
                record.sent_at = now
            record_outbox(
                session,
                organization_id=organization_id,
                aggregate_type="data_quality_incident",
                aggregate_id=str(incident.id),
                event_type="data_quality.incident.itsm_payload.v1",
                payload={**payload, "webhook_status": status, "webhook_error": error},
            )
        else:
            # EMAIL/WEBHOOK delivery transport is an infra concern (SMTP relay,
            # generic webhook fan-out) not built here; the routed event is
            # persisted as SENT so it is visible/acknowledgeable, matching
            # DQ-1's existing scope for those channels elsewhere in the platform.
            record.status = "SENT"
            record.sent_at = now
        session.add(record)
        created.append(record)

    if created:
        record_outbox(
            session,
            organization_id=organization_id,
            aggregate_type="data_quality_incident",
            aggregate_id=str(incident.id),
            event_type="data_quality.incident.notification_routed.v1",
            payload={
                "incident_id": str(incident.id),
                "severity": incident.severity,
                "events_routed": len(created),
                "channels": sorted({record.channel for record in created}),
            },
        )
    # NT-1: alongside DQ-1's rule-matched channels, push the incident to the
    # organization's Slack/Teams governance channel. Distinct from the rule
    # engine above: that routes to the owners a rule names, this is the
    # broadcast an operator watches.
    await notify_safely(
        session,
        organization_id,
        "QUALITY_INCIDENT_OPENED"
        if incident.status in ("OPEN", "REOPENED")
        else "QUALITY_INCIDENT_RESOLVED",
        {
            "object_type": "TABLE",
            "object_id": str(incident.table_id) if incident.table_id else None,
            "severity": incident.severity,
            "occurred_at": datetime.now(UTC).isoformat(),
        },
        settings=settings,
    )
    return created


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
        "certifications_expired": 0,
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

    # DQ-6 (+ its month-end follow-up): only read the extra scan history when at
    # least one seasonal strategy is on, so a tenant that has not opted into either
    # pays no additional query cost at all.
    settings = get_settings()
    seasonality_enabled = bool(settings.quality_seasonal_thresholds_enabled)
    month_end_seasonality_enabled = bool(settings.quality_seasonal_month_end_enabled)
    row_count_history_by_table: dict[UUID, list[tuple[datetime, int]]] = {}
    if seasonality_enabled or month_end_seasonality_enabled:
        history_alias = aliased(TableProfile, baseline_rank)
        history_rows = (
            await session.scalars(
                select(history_alias).where(
                    baseline_rank.c.baseline_rank <= _SEASONALITY_HISTORY_LOOKBACK
                )
            )
        ).all()
        for history_profile in history_rows:
            row_count = _row_count(history_profile)
            if row_count is None:
                continue
            row_count_history_by_table.setdefault(history_profile.table_id, []).append(
                (history_profile.created_at, row_count)
            )

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

    # DQ-1: routing rules read once per run, same idiom as `policies` above --
    # an org that has configured none pays no per-incident query cost
    # (`route_and_notify_incident` short-circuits on an empty list).
    notification_rules = (
        await session.scalars(
            select(NotificationRuleRecord).where(
                NotificationRuleRecord.organization_id == organization_id,
                NotificationRuleRecord.enabled.is_(True),
            )
        )
    ).all()
    newly_open_incidents: list[DataQualityIncident] = []

    now = datetime.now(UTC)
    for profile in profiles:
        baseline = baseline_by_table.get(profile.table_id)
        policy = policy_by_table.get(profile.table_id) or default_policy
        snapshot = policy_snapshot(policy)
        result = evaluate_quality(
            _snapshot(profile, columns_by_profile.get(profile.id, [])),
            _snapshot(baseline, columns_by_profile.get(baseline.id, [])) if baseline else None,
            snapshot,
            row_count_history=row_count_history_by_table.get(profile.table_id),
            current_observed_at=profile.created_at,
            seasonality_enabled=seasonality_enabled,
            seasonality_min_samples=settings.quality_seasonal_min_samples,
            seasonality_zscore_threshold=settings.quality_seasonal_zscore_threshold,
            month_end_seasonality_enabled=month_end_seasonality_enabled,
            month_end_window_days=settings.quality_seasonal_month_end_window_days,
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
                newly_open_incidents.append(current_incident)
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
                    newly_open_incidents.append(current_incident)

    # DQ-1: route each newly-opened/reopened incident through the notification
    # engine (and, for ITSM-channel matches, the real webhook emitter) after
    # the profiling loop so every incident already has its final `id`/fields
    # settled. A `flush()` ensures each incident's row exists for
    # `NotificationEventRecord.incident_id`'s FK before it is referenced.
    if newly_open_incidents and notification_rules:
        await session.flush()
        for incident in newly_open_incidents:
            await route_and_notify_incident(
                session,
                incident,
                organization_id=organization_id,
                notification_rules=list(notification_rules),
                settings=settings,
            )

    # DQ-3 (module 11 sec 9's fifth coupling row, off by default -- see the
    # setting's own comment in config.py): `incident_by_control` holds every
    # incident touched by this run at its final, post-loop status (resolved
    # incidents flipped to RESOLVED above, reopened ones flipped back to
    # OPEN), so it -- not the pre-loop `active_by_table` snapshot -- is the
    # correct source for "does this table currently have a sustained run of
    # unresolved incidents". A brand-new incident's `status` column has a
    # server/client default ("OPEN") that SQLAlchemy only applies at flush
    # time, not at construction -- reading `.status` off an unflushed new
    # `DataQualityIncident` here would see `None`, not "OPEN". The DQ-1 block
    # above only flushes when it has notification rules to route through, so
    # this cannot rely on that having already happened.
    if settings.quality_certification_expiry_enabled:
        await session.flush()
        current_incidents = [
            IncidentSummary(
                incident_id=str(incident.id),
                asset_id=str(incident.table_id),
                severity=incident.severity,
                status=incident.status,
                anomaly_type=incident.anomaly_type,
            )
            for incident in incident_by_control.values()
        ]
        expired_certifications = await expire_sustained_incident_certifications(
            session,
            organization_id=organization_id,
            table_ids=table_ids,
            incidents=current_incidents,
            context=context,
            sustained_threshold=settings.quality_certification_sustained_threshold,
        )
        counts["certifications_expired"] = len(expired_certifications)

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
