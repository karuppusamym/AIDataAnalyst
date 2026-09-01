"""DQ-4: custom quality rule packs, evaluated on their own schedule.

``quality_service.evaluate_analysis_run`` (DQ-3's upstream) only ever runs
the three built-in deterministic controls (volume, null-rate, schema
fingerprint), and only when a profiling analysis run completes -- so a
steward's own thresholds ("this table must never drop below 1,000 rows")
have no home, and no check ever runs *between* scans. This module adds that:
a ``QualityRulePack`` groups named ``QualityRule`` threshold checks and runs
on its own ``interval_minutes`` cadence (``run_due_rule_packs``, wired into
``aida.workflows.scheduler.run_scheduler_iteration``), independent of the
Temporal profiling DAG.

Each rule is evaluated against the most recently *stored* profile snapshot
(``TableProfile``/``ColumnProfile``) rather than live source data, so a sweep
needs no query-gateway execution and stays value-free (INV-6) the same way
the built-in controls in ``data_quality.py`` do. A rule pack whose table has
no profile yet is simply skipped for that table (``NO_PROFILE_DATA``), not
treated as a pass or a failure.

Violations open/reopen the *same* ``DataQualityIncident`` rows the built-in
controls use, keyed by a rule-specific ``anomaly_type`` (``CUSTOM_RULE:<rule
id>``). ``quality_coupling.fetch_open_incidents`` filters only on
``datasource_id``/``table_id``/``status`` -- never ``anomaly_type`` -- so a
custom-rule incident demotes retrieval ranking, gates governed tools, and
attaches answer trust warnings exactly like a built-in one, through the
DQ-3 wiring that already exists. Nothing in this module touches those
call sites.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from aida.db import session_factory
from aida.events import record_audit, record_outbox
from aida.models import (
    ColumnProfile,
    DataQualityIncident,
    QualityRule,
    QualityRulePack,
    TableProfile,
)
from aida.security import SecurityContext

RULE_TYPES: tuple[str, ...] = (
    "TABLE_ROW_COUNT_MIN",
    "TABLE_ROW_COUNT_MAX",
    "COLUMN_NULL_RATE_MAX",
)

_SCHEDULER_PRINCIPAL = "fleet-scheduler"
logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """``passed`` is ``None`` when there is not yet enough profile data to judge."""

    passed: bool | None
    evidence: dict[str, Any]


def _row_count(profile: TableProfile) -> int | None:
    return (
        profile.row_count_estimate
        if profile.row_count_estimate is not None
        else profile.sampled_row_count
    )


def evaluate_rule(
    rule: QualityRule,
    *,
    profile: TableProfile | None,
    column_profile: ColumnProfile | None,
) -> RuleEvaluation:
    """Pure, value-free threshold check. No DB access, no source SQL."""
    if rule.rule_type == "TABLE_ROW_COUNT_MIN":
        count = _row_count(profile) if profile else None
        if count is None:
            return RuleEvaluation(None, {"reason": "NO_PROFILE_DATA"})
        evidence = {"row_count": count, "threshold": rule.threshold}
        return RuleEvaluation(count >= rule.threshold, evidence)

    if rule.rule_type == "TABLE_ROW_COUNT_MAX":
        count = _row_count(profile) if profile else None
        if count is None:
            return RuleEvaluation(None, {"reason": "NO_PROFILE_DATA"})
        evidence = {"row_count": count, "threshold": rule.threshold}
        return RuleEvaluation(count <= rule.threshold, evidence)

    if rule.rule_type == "COLUMN_NULL_RATE_MAX":
        if column_profile is None:
            return RuleEvaluation(None, {"reason": "NO_PROFILE_DATA"})
        total = column_profile.null_count + column_profile.non_null_count
        null_rate = column_profile.null_count / total if total else 0.0
        return RuleEvaluation(
            null_rate <= rule.threshold,
            {"null_rate": round(null_rate, 6), "threshold": rule.threshold},
        )

    raise ValueError(f"Unknown rule_type: {rule.rule_type}")


def rule_severity(evaluation: RuleEvaluation, rule: QualityRule) -> str:
    """CRITICAL when the observed value is at least double the configured
    tolerance past the threshold; WARNING otherwise. Mirrors
    ``data_quality._severity``'s "how far past the line" heuristic rather
    than inventing a second severity model for custom rules.
    """
    if rule.rule_type == "TABLE_ROW_COUNT_MIN":
        observed = evaluation.evidence.get("row_count", 0)
        return "CRITICAL" if rule.threshold > 0 and observed <= rule.threshold / 2 else "WARNING"
    if rule.rule_type == "TABLE_ROW_COUNT_MAX":
        observed = evaluation.evidence.get("row_count", 0)
        return "CRITICAL" if observed >= rule.threshold * 2 else "WARNING"
    observed = evaluation.evidence.get("null_rate", 0.0)
    return "CRITICAL" if observed >= min(rule.threshold * 2, 1.0) else "WARNING"


def _incident_fingerprint(organization_id: UUID, table_id: UUID, rule_id: UUID) -> str:
    material = f"{organization_id}:{table_id}:custom_rule:{rule_id}".encode()
    return hashlib.sha256(material).hexdigest()


def _anomaly_type(rule_id: UUID) -> str:
    return f"CUSTOM_RULE:{rule_id}"


async def _latest_profiles(
    session: AsyncSession, *, datasource_id: UUID, table_ids: set[UUID]
) -> dict[UUID, TableProfile]:
    if not table_ids:
        return {}
    ranked = (
        select(
            TableProfile,
            func.row_number()
            .over(partition_by=TableProfile.table_id, order_by=TableProfile.created_at.desc())
            .label("rank"),
        )
        .where(TableProfile.datasource_id == datasource_id, TableProfile.table_id.in_(table_ids))
        .subquery()
    )
    profile_alias = aliased(TableProfile, ranked)
    rows = (await session.scalars(select(profile_alias).where(ranked.c.rank == 1))).all()
    return {profile.table_id: profile for profile in rows}


async def evaluate_rule_pack(
    session: AsyncSession,
    *,
    rule_pack: QualityRulePack,
    rules: list[QualityRule],
    context: SecurityContext,
    now: datetime | None = None,
) -> dict[str, int]:
    """Evaluate every enabled rule in one pack against the latest stored
    profile snapshot, opening/reopening/resolving ``DataQualityIncident``
    rows exactly like ``quality_service.evaluate_analysis_run`` does for the
    built-in controls -- so DQ-3's runtime coupling picks up a custom-rule
    incident with no changes on its side.
    """
    effective_now = now or datetime.now(UTC)
    counts = {
        "rules_evaluated": 0,
        "skipped_no_data": 0,
        "incidents_opened": 0,
        "incidents_resolved": 0,
    }
    enabled_rules = [rule for rule in rules if rule.enabled]
    if not enabled_rules:
        return counts

    table_ids = {rule.table_id for rule in enabled_rules}
    profile_by_table = await _latest_profiles(
        session, datasource_id=rule_pack.datasource_id, table_ids=table_ids
    )
    profile_ids = [profile.id for profile in profile_by_table.values()]
    column_rules = [rule for rule in enabled_rules if rule.column_id is not None]
    column_profile_by_key: dict[tuple[UUID, UUID], ColumnProfile] = {}
    if profile_ids and column_rules:
        column_profile_rows = (
            await session.scalars(
                select(ColumnProfile).where(
                    ColumnProfile.table_profile_id.in_(profile_ids),
                    ColumnProfile.column_id.in_({rule.column_id for rule in column_rules}),
                )
            )
        ).all()
        column_profile_by_key = {
            (row.table_profile_id, row.column_id): row for row in column_profile_rows
        }

    anomaly_types = [_anomaly_type(rule.id) for rule in enabled_rules]
    existing_incidents = (
        await session.scalars(
            select(DataQualityIncident).where(
                DataQualityIncident.datasource_id == rule_pack.datasource_id,
                DataQualityIncident.anomaly_type.in_(anomaly_types),
            )
        )
    ).all()
    incident_by_anomaly_type = {incident.anomaly_type: incident for incident in existing_incidents}

    for rule in enabled_rules:
        profile = profile_by_table.get(rule.table_id)
        column_profile = (
            column_profile_by_key.get((profile.id, rule.column_id))
            if profile and rule.column_id
            else None
        )
        evaluation = evaluate_rule(rule, profile=profile, column_profile=column_profile)
        counts["rules_evaluated"] += 1
        if evaluation.passed is None:
            counts["skipped_no_data"] += 1
            continue

        anomaly_type = _anomaly_type(rule.id)
        current_incident = incident_by_anomaly_type.get(anomaly_type)

        if evaluation.passed:
            if current_incident is not None and current_incident.status != "RESOLVED":
                current_incident.status = "RESOLVED"
                current_incident.resolved_by = "quality-rule-engine"
                current_incident.resolved_at = effective_now
                current_incident.resolution_reason = (
                    "Custom rule condition returned within the configured threshold."
                )
                counts["incidents_resolved"] += 1
            continue

        severity = rule_severity(evaluation, rule)
        label = rule.name or rule.rule_type.replace("_", " ").lower()
        if current_incident is None:
            current_incident = DataQualityIncident(
                organization_id=rule_pack.organization_id,
                datasource_id=rule_pack.datasource_id,
                table_id=rule.table_id,
                policy_id=None,
                fingerprint=_incident_fingerprint(
                    rule_pack.organization_id, rule.table_id, rule.id
                ),
                anomaly_type=anomaly_type,
                severity=severity,
                status="OPEN",
                summary=f"Custom rule '{label}' failed its configured threshold.",
                evidence=evaluation.evidence,
                first_observed_at=effective_now,
                last_observed_at=effective_now,
            )
            session.add(current_incident)
            incident_by_anomaly_type[anomaly_type] = current_incident
            counts["incidents_opened"] += 1
        else:
            reopened = current_incident.status == "RESOLVED"
            current_incident.severity = severity
            current_incident.status = "OPEN"
            current_incident.evidence = evaluation.evidence
            current_incident.last_observed_at = effective_now
            current_incident.occurrence_count += 1
            current_incident.resolved_by = None
            current_incident.resolved_at = None
            current_incident.resolution_reason = None
            if reopened:
                counts["incidents_opened"] += 1

    await session.flush()
    record_audit(
        session,
        context,
        action="data_quality.custom_rule_pack.evaluate",
        resource_type="quality_rule_pack",
        resource_id=str(rule_pack.id),
        outcome="SUCCESS",
        correlation_id=str(rule_pack.id),
        details=counts,
    )
    record_outbox(
        session,
        organization_id=rule_pack.organization_id,
        aggregate_type="quality_rule_pack",
        aggregate_id=str(rule_pack.id),
        event_type="data_quality.custom_rule_pack.evaluated.v1",
        payload={
            "rule_pack_id": str(rule_pack.id),
            "datasource_id": str(rule_pack.datasource_id),
            **counts,
        },
    )
    return counts


def rule_pack_due(last_run_at: datetime | None, now: datetime, interval_minutes: int) -> bool:
    """Whether a rule pack sweep is due. ``None`` (never swept) is always due."""
    if last_run_at is None:
        return True
    return (now - last_run_at).total_seconds() >= interval_minutes * 60


async def run_due_rule_packs(
    *, now: datetime | None = None, last_run_at: dict[UUID, datetime]
) -> int:
    """Sweep every enabled rule pack across every organization that is due
    per its own ``interval_minutes`` (tracked in ``last_run_at``, keyed by
    rule pack id -- the same in-process-memory, restart-just-resweeps
    approach GL-6's owner-routing cadence already uses in
    ``aida.workflows.scheduler``, since there is no per-pack "next due at"
    column and a sweep is idempotent/safe to repeat).

    One rule pack's failure is logged and skipped, matching
    ``run_owner_routing_pass``'s fault isolation, so a bad rule pack never
    blocks every other organization's sweep in the same iteration.
    """
    effective_now = now or datetime.now(UTC)
    swept = 0
    async with session_factory() as session:
        rule_packs = (
            await session.scalars(select(QualityRulePack).where(QualityRulePack.enabled.is_(True)))
        ).all()
        due_packs = [
            pack
            for pack in rule_packs
            if rule_pack_due(last_run_at.get(pack.id), effective_now, pack.interval_minutes)
        ]

    for pack in due_packs:
        try:
            async with session_factory() as session:
                rules = (
                    await session.scalars(
                        select(QualityRule).where(QualityRule.rule_pack_id == pack.id)
                    )
                ).all()
                live_pack = await session.get(QualityRulePack, pack.id)
                assert live_pack is not None
                worker_context = SecurityContext(
                    principal_id=_SCHEDULER_PRINCIPAL,
                    principal_type="WORKER",
                    organization_id=live_pack.organization_id,
                    roles=frozenset({"SchedulerWorker"}),
                )
                await evaluate_rule_pack(
                    session,
                    rule_pack=live_pack,
                    rules=list(rules),
                    context=worker_context,
                    now=effective_now,
                )
                await session.commit()
        except Exception:
            logger.exception("custom_rule_pack_sweep_failed", rule_pack_id=str(pack.id))
            continue
        last_run_at[pack.id] = effective_now
        swept += 1
    return swept
