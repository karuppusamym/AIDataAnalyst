"""Tests for compliance pack generation."""

import hashlib
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aida.compliance_packs import (
    CompliancePack,
    ComplianceSection,
    EvidenceItem,
    _compute_checksum,
    _section_to_dict,
)


# ---------------------------------------------------------------------------
# Checksum reproducibility
# ---------------------------------------------------------------------------


def test_checksum_reproducibility() -> None:
    """Same inputs produce the same checksum (packs are reproducible)."""
    pack_dict = {
        "name": "MODEL_RISK Compliance Pack",
        "framework": "MODEL_RISK",
        "period_start": "2025-01-01T00:00:00",
        "period_end": "2025-01-31T23:59:59",
        "sections": [],
    }
    cs1 = _compute_checksum(pack_dict)
    cs2 = _compute_checksum(pack_dict)
    assert cs1 == cs2


def test_checksum_differs_on_different_inputs() -> None:
    """Different inputs produce different checksums."""
    pack_a = {
        "name": "MODEL_RISK Compliance Pack",
        "framework": "MODEL_RISK",
        "period_start": "2025-01-01",
        "period_end": "2025-01-31",
        "sections": [],
    }
    pack_b = dict(pack_a, framework="BCBS_239")
    assert _compute_checksum(pack_a) != _compute_checksum(pack_b)


def test_checksum_ignores_generated_at() -> None:
    """Checksum is independent of generated_at for reproducibility."""
    pack_a = {
        "name": "Test Pack",
        "framework": "AI_USAGE",
        "sections": [],
        "generated_at": "2025-01-01T00:00:00",
    }
    pack_b = dict(pack_a, generated_at="2025-06-01T12:00:00")
    assert _compute_checksum(pack_a) == _compute_checksum(pack_b)


# ---------------------------------------------------------------------------
# Section serialization
# ---------------------------------------------------------------------------


def test_section_to_dict_roundtrip() -> None:
    section = ComplianceSection(
        title="Model Route Inventory",
        control_id="MR-001",
        evidence=[
            EvidenceItem(
                source="model_route_configuration",
                count=5,
                summary="5 approved model routes",
            )
        ],
        status="COMPLIANT",
    )
    d = _section_to_dict(section)
    assert d["title"] == "Model Route Inventory"
    assert d["control_id"] == "MR-001"
    assert d["status"] == "COMPLIANT"
    assert len(d["evidence"]) == 1
    assert d["evidence"][0]["count"] == 5


# ---------------------------------------------------------------------------
# CompliancePack structure
# ---------------------------------------------------------------------------


def test_compliance_pack_all_fields() -> None:
    """CompliancePack holds all expected fields."""
    now = datetime.now(UTC)
    pack = CompliancePack(
        name="TEST Pack",
        framework="MODEL_RISK",
        period_start=now - timedelta(days=30),
        period_end=now,
        sections=[
            ComplianceSection(
                title="Test Section",
                control_id="T-001",
                evidence=[],
                status="COMPLIANT",
            )
        ],
        generated_at=now,
        checksum="abc123",
    )
    assert pack.name == "TEST Pack"
    assert pack.framework == "MODEL_RISK"
    assert len(pack.sections) == 1
    assert pack.checksum == "abc123"


def test_compliance_pack_empty_sections() -> None:
    now = datetime.now(UTC)
    pack = CompliancePack(
        name="Empty Pack",
        framework="BCBS_239",
        period_start=now - timedelta(days=30),
        period_end=now,
        sections=[],
        generated_at=now,
        checksum="empty",
    )
    assert len(pack.sections) == 0


# ---------------------------------------------------------------------------
# Framework coverage
# ---------------------------------------------------------------------------


def test_supported_frameworks() -> None:
    """All five frameworks have generators."""
    from aida.compliance_packs import _GENERATORS

    expected_frameworks = {"MODEL_RISK", "BCBS_239", "ACCESS_REVIEW", "AI_USAGE", "CHANGE_CONTROL"}
    assert set(_GENERATORS.keys()) == expected_frameworks


# ---------------------------------------------------------------------------
# Evidence item
# ---------------------------------------------------------------------------


def test_evidence_item_with_details() -> None:
    item = EvidenceItem(
        source="agent_run",
        count=42,
        summary="42 agent runs",
        details={"avg_duration_ms": 150},
    )
    assert item.source == "agent_run"
    assert item.count == 42
    assert item.details["avg_duration_ms"] == 150


def test_evidence_item_default_details() -> None:
    item = EvidenceItem(
        source="audit_event",
        count=0,
        summary="no events",
    )
    assert item.details == {}


# ---------------------------------------------------------------------------
# Section status
# ---------------------------------------------------------------------------


def test_section_status_values() -> None:
    """All three status values are valid."""
    for status in ("COMPLIANT", "NON_COMPLIANT", "NOT_ASSESSED"):
        section = ComplianceSection(
            title="Test",
            control_id="X-001",
            evidence=[],
            status=status,
        )
        assert section.status == status
