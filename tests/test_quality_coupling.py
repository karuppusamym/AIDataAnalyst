from aida.quality_coupling import (
    IncidentSummary,
    check_quality_gate,
    check_tool_gate,
    demote_in_retrieval,
    get_trust_warning,
    should_expire_certification,
)


def _incident(
    incident_id: str = "inc-1",
    *,
    asset_id: str = "asset-1",
    severity: str = "WARNING",
    status: str = "OPEN",
    anomaly_type: str = "NULL_RATE_SHIFT",
) -> IncidentSummary:
    return IncidentSummary(
        incident_id=incident_id,
        asset_id=asset_id,
        severity=severity,
        status=status,
        anomaly_type=anomaly_type,
    )


# --- check_quality_gate ---


def test_no_gate_when_no_incidents() -> None:
    result = check_quality_gate("asset-1", [])
    assert result is None


def test_no_gate_when_all_resolved() -> None:
    incidents = [_incident(status="RESOLVED")]
    result = check_quality_gate("asset-1", incidents)
    assert result is None


def test_block_on_critical_incident() -> None:
    incidents = [_incident(severity="CRITICAL")]
    result = check_quality_gate("asset-1", incidents)
    assert result is not None
    assert result.gate_action == "BLOCK"
    assert result.incident_severity == "CRITICAL"


def test_demote_on_warning_incident() -> None:
    incidents = [_incident(severity="WARNING")]
    result = check_quality_gate("asset-1", incidents)
    assert result is not None
    assert result.gate_action == "DEMOTE"
    assert result.incident_severity == "WARNING"


def test_gate_scoped_to_asset() -> None:
    incidents = [_incident(asset_id="other-asset")]
    result = check_quality_gate("asset-1", incidents)
    assert result is None


# --- demote_in_retrieval ---


def test_no_demotion_when_clean() -> None:
    factor = demote_in_retrieval("asset-1", [])
    assert factor == 1.0


def test_critical_demotion() -> None:
    incidents = [_incident(severity="CRITICAL")]
    factor = demote_in_retrieval("asset-1", incidents)
    assert factor == 0.3


def test_warning_demotion_scales_with_count() -> None:
    one_warning = [_incident(severity="WARNING")]
    two_warnings = [
        _incident(incident_id="inc-1", severity="WARNING"),
        _incident(incident_id="inc-2", severity="WARNING"),
    ]
    assert demote_in_retrieval("asset-1", one_warning) == 0.85
    assert demote_in_retrieval("asset-1", two_warnings) == 0.7


def test_demotion_floors_at_half() -> None:
    many_warnings = [
        _incident(incident_id=f"inc-{i}", severity="WARNING") for i in range(10)
    ]
    factor = demote_in_retrieval("asset-1", many_warnings)
    assert factor == 0.5


# --- get_trust_warning ---


def test_no_warning_when_clean() -> None:
    result = get_trust_warning("asset-1", [])
    assert result is None


def test_trust_warning_critical() -> None:
    incidents = [_incident(severity="CRITICAL")]
    result = get_trust_warning("asset-1", incidents)
    assert result is not None
    assert result.severity == "CRITICAL"
    assert "1 active quality incident" in result.message
    assert result.incident_ids == ["inc-1"]


def test_trust_warning_multiple() -> None:
    incidents = [
        _incident(incident_id="inc-1", severity="WARNING"),
        _incident(incident_id="inc-2", severity="WARNING"),
    ]
    result = get_trust_warning("asset-1", incidents)
    assert result is not None
    assert result.severity == "WARNING"
    assert "2 active quality incidents" in result.message


# --- check_tool_gate ---


def test_tool_gate_allow_when_clean() -> None:
    result = check_tool_gate("tool-1", ["asset-1"], [])
    assert result.action == "ALLOW"
    assert result.affected_assets == []


def test_tool_gate_block_on_critical_dependency() -> None:
    incidents = [_incident(asset_id="asset-1", severity="CRITICAL")]
    result = check_tool_gate("tool-1", ["asset-1", "asset-2"], incidents)
    assert result.action == "BLOCK"
    assert "asset-1" in result.affected_assets
    assert "critical" in result.message.lower()


def test_tool_gate_warn_on_warning_dependency() -> None:
    incidents = [_incident(asset_id="asset-1", severity="WARNING")]
    result = check_tool_gate("tool-1", ["asset-1"], incidents)
    assert result.action == "WARN"
    assert "asset-1" in result.affected_assets


def test_tool_gate_only_checks_listed_dependencies() -> None:
    incidents = [_incident(asset_id="asset-2", severity="CRITICAL")]
    result = check_tool_gate("tool-1", ["asset-1"], incidents)
    assert result.action == "ALLOW"


# --- should_expire_certification ---


def test_no_expiry_below_threshold() -> None:
    incidents = [
        _incident(incident_id="inc-1"),
        _incident(incident_id="inc-2"),
    ]
    assert should_expire_certification("asset-1", incidents) is False


def test_expiry_at_threshold() -> None:
    incidents = [
        _incident(incident_id=f"inc-{i}") for i in range(3)
    ]
    assert should_expire_certification("asset-1", incidents) is True


def test_expiry_custom_threshold() -> None:
    incidents = [_incident(incident_id="inc-1")]
    assert should_expire_certification("asset-1", incidents, sustained_threshold=1) is True


def test_expiry_ignores_resolved() -> None:
    incidents = [
        _incident(incident_id="inc-1", status="OPEN"),
        _incident(incident_id="inc-2", status="OPEN"),
        _incident(incident_id="inc-3", status="RESOLVED"),
    ]
    assert should_expire_certification("asset-1", incidents) is False
