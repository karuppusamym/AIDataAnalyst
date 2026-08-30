"""Quality-runtime coupling (DQ-3).

Integrates quality incident status with runtime decisions: demotion in
retrieval ranking, trust warnings on answers, and tool gating when
upstream dependencies have quality incidents.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class QualityGate:
    """Gate result for an asset with quality issues."""

    asset_id: str
    incident_severity: str
    gate_action: str  # DEMOTE, WARN, BLOCK


@dataclass(frozen=True, slots=True)
class TrustWarning:
    """Warning surfaced when answers use affected assets."""

    asset_id: str
    message: str
    severity: str
    incident_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ToolGateResult:
    """Gate result for a tool whose dependencies have incidents."""

    tool_id: str
    action: str  # ALLOW, WARN, BLOCK
    affected_assets: list[str] = field(default_factory=list)
    message: str = ""


@dataclass(frozen=True, slots=True)
class IncidentSummary:
    """Simplified incident data for coupling decisions."""

    incident_id: str
    asset_id: str
    severity: str
    status: str  # OPEN, ACKNOWLEDGED, RESOLVED
    anomaly_type: str


def check_quality_gate(
    asset_id: str, incidents: list[IncidentSummary]
) -> QualityGate | None:
    """Check if an asset has active quality incidents warranting a gate action.

    Returns None if no quality gate should be applied.
    """
    active = [
        i for i in incidents
        if i.asset_id == asset_id and i.status in ("OPEN", "ACKNOWLEDGED")
    ]
    if not active:
        return None

    has_critical = any(i.severity == "CRITICAL" for i in active)
    if has_critical:
        return QualityGate(
            asset_id=asset_id,
            incident_severity="CRITICAL",
            gate_action="BLOCK",
        )

    return QualityGate(
        asset_id=asset_id,
        incident_severity="WARNING",
        gate_action="DEMOTE",
    )


def demote_in_retrieval(
    asset_id: str, incidents: list[IncidentSummary]
) -> float:
    """Return a demotion factor (0.0-1.0) for retrieval ranking.

    1.0 means no demotion, lower values push the asset down in results.
    """
    active = [
        i for i in incidents
        if i.asset_id == asset_id and i.status in ("OPEN", "ACKNOWLEDGED")
    ]
    if not active:
        return 1.0

    has_critical = any(i.severity == "CRITICAL" for i in active)
    if has_critical:
        return 0.3

    warning_count = sum(1 for i in active if i.severity == "WARNING")
    return max(0.5, 1.0 - (warning_count * 0.15))


def get_trust_warning(
    asset_id: str, incidents: list[IncidentSummary]
) -> TrustWarning | None:
    """Generate a trust warning for answers using affected assets."""
    active = [
        i for i in incidents
        if i.asset_id == asset_id and i.status in ("OPEN", "ACKNOWLEDGED")
    ]
    if not active:
        return None

    has_critical = any(i.severity == "CRITICAL" for i in active)
    severity = "CRITICAL" if has_critical else "WARNING"
    incident_count = len(active)
    message = (
        f"Asset {asset_id} has {incident_count} active quality "
        f"incident{'s' if incident_count > 1 else ''} "
        f"(highest severity: {severity}). Results may be unreliable."
    )
    return TrustWarning(
        asset_id=asset_id,
        message=message,
        severity=severity,
        incident_ids=[i.incident_id for i in active],
    )


def check_tool_gate(
    tool_id: str,
    dependency_asset_ids: list[str],
    incidents: list[IncidentSummary],
) -> ToolGateResult:
    """Check whether a tool should be blocked/warned based on dependency incidents."""
    affected: list[str] = []
    max_severity = "INFO"

    for asset_id in dependency_asset_ids:
        active = [
            i for i in incidents
            if i.asset_id == asset_id and i.status in ("OPEN", "ACKNOWLEDGED")
        ]
        if active:
            affected.append(asset_id)
            if any(i.severity == "CRITICAL" for i in active):
                max_severity = "CRITICAL"
            elif max_severity != "CRITICAL":
                max_severity = "WARNING"

    if not affected:
        return ToolGateResult(tool_id=tool_id, action="ALLOW")

    if max_severity == "CRITICAL":
        action = "BLOCK"
        message = (
            f"Tool {tool_id} blocked: {len(affected)} upstream "
            f"asset(s) have critical quality incidents."
        )
    else:
        action = "WARN"
        message = (
            f"Tool {tool_id} warning: {len(affected)} upstream "
            f"asset(s) have quality incidents."
        )

    return ToolGateResult(
        tool_id=tool_id,
        action=action,
        affected_assets=affected,
        message=message,
    )


def should_expire_certification(
    asset_id: str,
    incidents: list[IncidentSummary],
    *,
    sustained_threshold: int = 3,
) -> bool:
    """Determine whether sustained incidents should expire asset certification.

    An asset's certification expires when the number of unresolved incidents
    reaches or exceeds the sustained threshold.
    """
    active = [
        i for i in incidents
        if i.asset_id == asset_id and i.status in ("OPEN", "ACKNOWLEDGED")
    ]
    return len(active) >= sustained_threshold
