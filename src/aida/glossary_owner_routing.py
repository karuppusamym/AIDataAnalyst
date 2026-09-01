"""Unowned-asset backlog owner routing and escalation (GL-6).

Coverage scoring (GL-4) already returns a bounded backlog of unowned table
IDs (``StewardshipCoverageRead.unowned_table_ids``) and the Stewardship
Control Center already surfaces it. What is missing is automated routing: a
table that sits unowned past a bound should be surfaced to a candidate owner
(or a stewardship-lead escalation contact), and escalated further if it is
still unaddressed.

Rather than build a second notification/escalation/ITSM-webhook mechanism,
this module reuses DQ-1's generic routing engine (``aida.notification_routing``)
as-is: an unowned table becomes the same ``Incident`` shape a data-quality
incident is, matched against the same org-scoped ``NotificationRuleRecord``
rows, through the same ``route_notification`` / ``should_escalate`` /
``escalate`` / ``format_itsm_payload`` functions. Only persistence differs --
``NotificationEventRecord`` is FK-scoped to ``data_quality_incident`` and
cannot carry a table subject, so routing/escalation outcomes for unowned
assets are recorded on ``UnownedAssetEscalation`` instead, using the same
status vocabulary and dedup key the engine already produces.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from fnmatch import fnmatchcase
from uuid import UUID

from aida.models import NotificationRuleRecord, OwnershipRule, UnownedAssetEscalation
from aida.notification_routing import (
    Incident,
    NotificationEvent,
    NotificationRule,
    escalate,
    format_itsm_payload,
    route_notification,
    should_escalate,
)

# Defaults: an unowned table is first routed after a week unaddressed, and
# escalated further after two weeks unaddressed unless the matched
# notification rule specifies its own `escalation_after_minutes`.
DEFAULT_ROUTE_AFTER = timedelta(days=7)
DEFAULT_ESCALATE_AFTER = timedelta(days=14)
# GL-6 tier 2: an entry still unresolved this long *after its tier-1
# escalation* (not from first-detected) escalates again, unconditionally
# through ITSM regardless of what channel tier 1 used -- the single-tier
# engine has no further tier of its own to fall back to, so tier 2 is a fixed
# "make sure this becomes an operational ticket" backstop rather than a
# second configurable notification rule.
DEFAULT_ESCALATE_TIER2_AFTER = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class TableFacts:
    """The bare facts about a table needed to route/escalate its backlog entry."""

    table_id: UUID
    datasource_id: UUID
    table_name: str
    schema_name: str
    domain_key: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def qualified_name(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


@dataclass(frozen=True, slots=True)
class BacklogRoutingResult:
    """What one sync/route/escalate pass over the unowned backlog did."""

    created: list[UnownedAssetEscalation] = field(default_factory=list)
    routed: list[UnownedAssetEscalation] = field(default_factory=list)
    escalated: list[UnownedAssetEscalation] = field(default_factory=list)
    escalated_tier2: list[UnownedAssetEscalation] = field(default_factory=list)
    resolved: list[UnownedAssetEscalation] = field(default_factory=list)
    itsm_payloads: list[dict[str, object]] = field(default_factory=list)


def select_candidate_owner(facts: TableFacts, rules: list[OwnershipRule]) -> str | None:
    """Find a candidate owner for an unowned table from active ownership rules.

    Mirrors the match semantics ``apply_ownership_rule`` uses to bulk-assign
    ownership (TABLE_NAME/SCHEMA_NAME/QUALIFIED_NAME/DOMAIN_KEY/TAG glob
    matching over the first matching rule) without creating an assignment --
    routing only *suggests* an owner to notify; assignment still goes
    through the reviewed bulk-operation path.
    """
    for rule in rules:
        if rule.status != "ACTIVE":
            continue
        pattern = rule.match_pattern.casefold()
        if rule.match_field == "TAG":
            if any(fnmatchcase(tag.casefold(), pattern) for tag in facts.tags):
                return rule.owner_principal
            continue
        candidates = {
            "TABLE_NAME": facts.table_name,
            "SCHEMA_NAME": facts.schema_name,
            "QUALIFIED_NAME": facts.qualified_name,
            "DOMAIN_KEY": facts.domain_key,
        }
        value = candidates[rule.match_field]
        if value is not None and fnmatchcase(value.casefold(), pattern):
            return rule.owner_principal
    return None


def _incident_for(
    facts: TableFacts,
    *,
    first_detected_unowned_at: datetime,
    now: datetime,
    escalate_after: timedelta,
    candidate_owner: str | None,
) -> Incident:
    """Build the same ``Incident`` shape a data-quality incident routes as."""
    age = now - first_detected_unowned_at
    severity = "CRITICAL" if age >= escalate_after else "WARNING"
    return Incident(
        incident_id=f"unowned-asset-{facts.table_id}",
        fingerprint=f"unowned-asset:{facts.table_id}",
        severity=severity,
        source_id=str(facts.datasource_id),
        domain=facts.domain_key,
        owner=candidate_owner,
        message=(
            f"{facts.qualified_name} has had no active ownership assignment for "
            f"{age.days} day(s)."
        ),
    )


def _as_engine_rules(rules: list[NotificationRuleRecord]) -> list[NotificationRule]:
    """Adapt persisted org notification rules into the pure engine's rule shape."""
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


def _engine_event_for(
    entry: UnownedAssetEscalation, matched_rule: NotificationRule | None
) -> NotificationEvent:
    """Reconstruct the engine's ``NotificationEvent`` from a persisted entry.

    ``should_escalate``/``escalate`` operate on that dataclass, not on the
    ORM row, so a persisted ROUTED/ESCALATED entry is mapped back onto it --
    "SENT" for a routed-but-not-yet-escalated entry, acknowledged_at
    standing in for resolution.
    """
    return NotificationEvent(
        notification_id=f"notif-unowned-asset-{entry.table_id}",
        incident_id=str(entry.table_id),
        rule_id=str(entry.notification_rule_id) if matched_rule is None else matched_rule.rule_id,
        severity="CRITICAL" if entry.status == "ESCALATED" else "WARNING",
        source="",
        domain=None,
        owner=entry.candidate_owner,
        message="",
        channel=entry.channel or "EMAIL",
        recipients=list(entry.recipients),
        status="ESCALATED" if entry.status == "ESCALATED" else "SENT",
        dedup_key=entry.dedup_key or "",
        created_at=entry.first_detected_unowned_at,
        sent_at=entry.routed_at,
        escalated_at=entry.escalated_at,
        acknowledged_at=entry.resolved_at,
    )


def sync_unowned_asset_backlog(
    *,
    organization_id: UUID,
    unowned_table_ids: set[UUID],
    existing_entries: dict[UUID, UnownedAssetEscalation],
    table_facts: dict[UUID, TableFacts],
    ownership_rules: list[OwnershipRule],
    notification_rules: list[NotificationRuleRecord],
    now: datetime,
    route_after: timedelta = DEFAULT_ROUTE_AFTER,
    escalate_after: timedelta = DEFAULT_ESCALATE_AFTER,
    escalate_tier2_after: timedelta = DEFAULT_ESCALATE_TIER2_AFTER,
    route_limit: int = 500,
) -> BacklogRoutingResult:
    """Reconcile the unowned backlog, routing and escalating aged entries.

    ``unowned_table_ids`` must be the *complete* current unowned set for the
    scope (uncapped) -- it is what decides whether a previously tracked
    entry gets resolved, so truncating it would silently "resolve" tables
    that are still unowned but merely didn't make this pass's cut. Creating
    and routing new/aged entries is itself bounded to ``route_limit``
    (default 500, matching the cap coverage scoring already applies to the
    backlog it returns) processed in a stable order, so a single call does
    bounded work.

    ``existing_entries`` and ``table_facts`` are keyed by table_id; the
    caller loads them (one org-scoped query each) so this function stays a
    pure reconciliation step, in the same style as
    ``build_stewardship_coverage``. Entries are mutated in place -- new ones
    land in ``result.created`` and are the caller's responsibility to
    ``session.add``.

    Escalation has two tiers. Tier 1 (ROUTED -> ESCALATED) goes through the
    shared ``notification_routing`` engine exactly as before, on whatever
    channel the matched rule names. Tier 2 (ESCALATED -> ESCALATED_TIER_2)
    is this module's own backstop, not the engine's: an entry still
    unaddressed ``escalate_tier2_after`` past its *own* tier-1
    ``escalated_at`` (not from first-detected) unconditionally produces an
    ITSM payload, regardless of what channel tier 1 used -- a bank cannot
    let an unowned table sit unaddressed indefinitely just because the
    tier-1 notification channel had no external delivery configured (e.g.
    the empty-recipient default EMAIL rule ``ensure_default_unowned_backlog_notification_rule``
    seeds), so tier 2 always escalates operationally rather than repeating
    the same notification.
    """
    result = BacklogRoutingResult()
    engine_rules = _as_engine_rules(notification_rules)
    rules_by_id = {rule.rule_id: rule for rule in engine_rules}
    seen_dedup_keys: set[str] = set()

    for table_id in sorted(unowned_table_ids, key=str)[:route_limit]:
        entry = existing_entries.get(table_id)
        if entry is None or entry.status == "RESOLVED":
            entry = UnownedAssetEscalation(
                organization_id=organization_id,
                table_id=table_id,
                first_detected_unowned_at=now,
                status="PENDING",
                recipients=[],
            )
            existing_entries[table_id] = entry
            result.created.append(entry)

        facts = table_facts.get(table_id)
        if facts is None:
            continue

        age = now - entry.first_detected_unowned_at

        if entry.status == "PENDING" and age >= route_after:
            candidate_owner = select_candidate_owner(facts, ownership_rules)
            incident = _incident_for(
                facts,
                first_detected_unowned_at=entry.first_detected_unowned_at,
                now=now,
                escalate_after=escalate_after,
                candidate_owner=candidate_owner,
            )
            events = route_notification(incident, engine_rules, seen_dedup_keys=seen_dedup_keys)
            entry.candidate_owner = candidate_owner
            if events:
                chosen = events[0]
                entry.notification_rule_id = UUID(chosen.rule_id)
                entry.channel = chosen.channel
                entry.recipients = list(chosen.recipients)
                entry.dedup_key = chosen.dedup_key
                entry.routed_at = now
                entry.status = "ESCALATED" if incident.severity == "CRITICAL" else "ROUTED"
                if entry.status == "ESCALATED":
                    entry.escalated_at = now
                    result.escalated.append(entry)
                else:
                    result.routed.append(entry)
                if chosen.channel == "ITSM":
                    result.itsm_payloads.append(format_itsm_payload(incident))
            continue

        if entry.status == "ROUTED":
            matched_rule = rules_by_id.get(
                str(entry.notification_rule_id) if entry.notification_rule_id else ""
            )
            engine_event = _engine_event_for(entry, matched_rule)
            escalation_rule = matched_rule or NotificationRule(
                rule_id="unowned-asset-fallback",
                organization_id=str(organization_id),
                conditions={},
                channel=entry.channel or "EMAIL",
                recipients=list(entry.recipients),
                escalation_after_minutes=int(escalate_after.total_seconds() // 60),
            )
            if should_escalate(engine_event, escalation_rule):
                escalate(engine_event)
                entry.status = "ESCALATED"
                entry.escalated_at = engine_event.escalated_at
                result.escalated.append(entry)
            continue

        if entry.status == "ESCALATED" and entry.escalated_at is not None:
            if now - entry.escalated_at >= escalate_tier2_after:
                incident = _incident_for(
                    facts,
                    first_detected_unowned_at=entry.first_detected_unowned_at,
                    now=now,
                    escalate_after=escalate_after,
                    candidate_owner=entry.candidate_owner,
                )
                entry.status = "ESCALATED_TIER_2"
                entry.escalated_tier2_at = now
                result.escalated_tier2.append(entry)
                result.itsm_payloads.append(format_itsm_payload(incident))

    for table_id, entry in existing_entries.items():
        if entry.status != "RESOLVED" and table_id not in unowned_table_ids:
            entry.status = "RESOLVED"
            entry.resolved_at = now
            result.resolved.append(entry)

    return result
